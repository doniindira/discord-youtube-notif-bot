import discord
from discord.ext import commands, tasks
import sqlite3
from datetime import datetime, timedelta, timezone
import logging
import os
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import asyncio

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class YouTubeAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.youtube = build('youtube', 'v3', developerKey=api_key)
        self.db_path = '/app/data/youtube_bot.db' if os.path.exists('/app/data') else 'youtube_bot.db'
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database"""
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else '.', exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Monitored channels table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                notification_channel_id TEXT,
                youtube_channel_id TEXT,
                youtube_channel_name TEXT,
                youtube_username TEXT,
                added_at TEXT,
                UNIQUE(guild_id, notification_channel_id, youtube_channel_id)
            )
        ''')
        
        # Sent notifications table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_notifications (
                video_id TEXT,
                guild_id TEXT,
                sent_at TEXT,
                PRIMARY KEY (video_id, guild_id)
            )
        ''')
        
        # Last check tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS last_check (
                channel_id TEXT,
                guild_id TEXT,
                last_video_id TEXT,
                last_check_time TEXT,
                PRIMARY KEY (channel_id, guild_id)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def search_channel(self, username: str):
        """Search for YouTube channel by username"""
        try:
            # Remove @ if present
            username = username.replace('@', '').strip()
            
            # Search for channels
            response = self.youtube.search().list(
                q=username,
                type='channel',
                part='snippet',
                maxResults=5
            ).execute()
            
            # Try to find exact match
            for item in response['items']:
                channel_id = item['id']['channelId']
                channel_title = item['snippet']['title']
                
                # Get detailed channel info to check custom URL
                channel_details = self.youtube.channels().list(
                    part='snippet,statistics',
                    id=channel_id
                ).execute()
                
                if channel_details['items']:
                    channel_data = channel_details['items'][0]
                    custom_url = channel_data['snippet'].get('customUrl', '').lower()
                    
                    # Check if this is our target channel
                    if (custom_url == username.lower() or 
                        custom_url == f"@{username.lower()}" or
                        channel_title.lower() == username.lower()):
                        
                        return {
                            'channel_id': channel_id,
                            'channel_name': channel_title,
                            'subscriber_count': int(channel_data['statistics'].get('subscriberCount', 0))
                        }
            
            # If no exact match, return first result
            if response['items']:
                first_item = response['items'][0]
                channel_id = first_item['id']['channelId']
                
                channel_details = self.youtube.channels().list(
                    part='snippet,statistics',
                    id=channel_id
                ).execute()
                
                if channel_details['items']:
                    channel_data = channel_details['items'][0]
                    return {
                        'channel_id': channel_id,
                        'channel_name': channel_data['snippet']['title'],
                        'subscriber_count': int(channel_data['statistics'].get('subscriberCount', 0))
                    }
            
            return None
            
        except HttpError as e:
            logger.error(f"YouTube API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error searching channel: {e}")
            return None
    
    def get_latest_videos(self, channel_id: str, max_results: int = 5):
        """Get latest videos from channel"""
        try:
            # Get uploads playlist ID
            channel_response = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            ).execute()
            
            if not channel_response['items']:
                return []
            
            playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            
            # Get videos from playlist (removed the 'order' parameter)
            playlist_response = self.youtube.playlistItems().list(
                part='snippet',
                playlistId=playlist_id,
                maxResults=max_results
            ).execute()
            
            videos = []
            video_ids = []
            
            for item in playlist_response['items']:
                video_id = item['snippet']['resourceId']['videoId']
                video_ids.append(video_id)
                
                videos.append({
                    'video_id': video_id,
                    'title': item['snippet']['title'],
                    'description': item['snippet']['description'][:500],
                    'published_at': item['snippet']['publishedAt'],
                    'channel_title': item['snippet']['channelTitle'],
                    'channel_id': channel_id,
                    'thumbnail': item['snippet']['thumbnails'].get('maxres', {}).get('url') or 
                               item['snippet']['thumbnails'].get('high', {}).get('url') or
                               item['snippet']['thumbnails'].get('medium', {}).get('url', ''),
                    'url': f"https://www.youtube.com/watch?v={video_id}"
                })
            
            # Get additional video details (statistics, live status)
            if video_ids:
                video_details = self.youtube.videos().list(
                    part='statistics,liveStreamingDetails,contentDetails',
                    id=','.join(video_ids)
                ).execute()
                
                for i, detail in enumerate(video_details['items']):
                    if i < len(videos):
                        videos[i]['view_count'] = int(detail['statistics'].get('viewCount', 0))
                        videos[i]['like_count'] = int(detail['statistics'].get('likeCount', 0))
                        
                        # Check if live
                        live_details = detail.get('liveStreamingDetails', {})
                        videos[i]['is_live'] = (
                            'actualStartTime' in live_details and 
                            'actualEndTime' not in live_details
                        )
                        
                        # Duration
                        duration = detail['contentDetails']['duration']
                        videos[i]['duration'] = self.parse_duration(duration)
            
            # Sort videos by published date (newest first) since we can't use 'order' parameter
            videos.sort(key=lambda x: x['published_at'], reverse=True)
            
            return videos
            
        except Exception as e:
            logger.error(f"Error getting videos: {e}")
            return []
    
    def parse_duration(self, duration: str) -> str:
        """Parse ISO 8601 duration to readable format"""
        if not duration or not duration.startswith('PT'):
            return ''
        
        import re
        match = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
        if not match:
            return ''
        
        hours, minutes, seconds = match.groups()
        result = []
        
        if hours:
            result.append(f"{hours}h")
        if minutes:
            result.append(f"{minutes}m")
        if seconds:
            result.append(f"{seconds}s")
        
        return ' '.join(result) if result else ''

# Global YouTube API instance
yt_api = None

class YouTubeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor_task.start()
    
    def cog_unload(self):
        self.monitor_task.cancel()
    
    @discord.app_commands.command(name="add_channel", description="Add YouTube channel to monitor")
    @discord.app_commands.describe(
        channel_username="YouTube channel username (e.g., @pewdiepie or pewdiepie)",
        notification_channel="Discord channel for notifications (optional)"
    )
    async def add_channel(self, interaction: discord.Interaction, channel_username: str, 
                         notification_channel: discord.TextChannel = None):
        await interaction.response.defer()
        
        if notification_channel is None:
            notification_channel = interaction.channel
        
        # Check permissions
        if not notification_channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.followup.send("❌ Bot doesn't have permission to send messages to that channel!")
            return
        
        # Search for channel
        channel_info = yt_api.search_channel(channel_username)
        if not channel_info:
            await interaction.followup.send(f"❌ Could not find YouTube channel: `{channel_username}`")
            return
        
        # Add to database
        conn = sqlite3.connect(yt_api.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO monitored_channels 
                (guild_id, notification_channel_id, youtube_channel_id, youtube_channel_name, youtube_username, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                str(interaction.guild.id),
                str(notification_channel.id),
                channel_info['channel_id'],
                channel_info['channel_name'],
                channel_username,
                datetime.now().isoformat()
            ))
            
            conn.commit()
            
            embed = discord.Embed(
                title="✅ Channel Added Successfully!",
                description=f"**{channel_info['channel_name']}** will be monitored in {notification_channel.mention}",
                color=0x00ff00
            )
            embed.add_field(name="Channel ID", value=channel_info['channel_id'], inline=True)
            embed.add_field(name="Subscribers", value=f"{channel_info['subscriber_count']:,}", inline=True)
            
            await interaction.followup.send(embed=embed)
            
        except sqlite3.IntegrityError:
            await interaction.followup.send(f"⚠️ Channel **{channel_info['channel_name']}** is already being monitored!")
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            await interaction.followup.send("❌ Error occurred while adding channel.")
        finally:
            conn.close()
    
    @discord.app_commands.command(name="remove_channel", description="Remove YouTube channel from monitoring")
    @discord.app_commands.describe(channel_username="YouTube channel username to remove")
    async def remove_channel(self, interaction: discord.Interaction, channel_username: str):
        conn = sqlite3.connect(yt_api.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM monitored_channels 
            WHERE guild_id = ? AND (youtube_username = ? OR youtube_channel_name = ?)
        ''', (str(interaction.guild.id), channel_username, channel_username))
        
        if cursor.rowcount > 0:
            conn.commit()
            await interaction.response.send_message(f"✅ Channel `{channel_username}` removed from monitoring!")
        else:
            await interaction.response.send_message(f"❌ Channel `{channel_username}` not found in monitoring list!")
        
        conn.close()
    
    @discord.app_commands.command(name="list_channels", description="Show monitored channels")
    async def list_channels(self, interaction: discord.Interaction):
        conn = sqlite3.connect(yt_api.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT youtube_channel_name, youtube_username, notification_channel_id
            FROM monitored_channels 
            WHERE guild_id = ?
            ORDER BY added_at DESC
        ''', (str(interaction.guild.id),))
        
        channels = cursor.fetchall()
        conn.close()
        
        if not channels:
            await interaction.response.send_message("📝 No channels are being monitored in this server.")
            return
        
        embed = discord.Embed(
            title="📺 Monitored YouTube Channels",
            color=0xff0000
        )
        
        for i, (name, username, channel_id) in enumerate(channels[:10], 1):
            channel = self.bot.get_channel(int(channel_id))
            channel_mention = channel.mention if channel else "Deleted Channel"
            
            embed.add_field(
                name=f"{i}. {name}",
                value=f"Username: `{username}`\nNotifications: {channel_mention}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="test", description="Test notification with latest video")
    @discord.app_commands.describe(channel_username="YouTube channel username to test")
    async def test_notification(self, interaction: discord.Interaction, channel_username: str):
        await interaction.response.defer()
        
        channel_info = yt_api.search_channel(channel_username)
        if not channel_info:
            await interaction.followup.send(f"❌ Channel `{channel_username}` not found!")
            return
        
        videos = yt_api.get_latest_videos(channel_info['channel_id'], 1)
        if not videos:
            await interaction.followup.send("❌ Could not get videos from this channel!")
            return
        
        video = videos[0]
        embed = self.create_video_embed(video, is_test=True)
        await interaction.followup.send("🧪 **TEST NOTIFICATION**", embed=embed)
    
    def create_video_embed(self, video, is_test=False):
        """Create Discord embed for video notification"""
        title_prefix = "🔴 LIVE" if video.get('is_live') else "📹 NEW VIDEO"
        if is_test:
            title_prefix = "🧪 TEST - " + title_prefix
        
        embed = discord.Embed(
            title=video['title'][:256],
            url=video['url'],
            description=video.get('description', '')[:500],
            color=0xff0000 if video.get('is_live') else 0x00ff00
        )
        
        embed.set_author(
            name=f"{title_prefix} - {video['channel_title']}",
            icon_url="https://www.youtube.com/favicon.ico"
        )
        
        if video.get('thumbnail'):
            embed.set_image(url=video['thumbnail'])
        
        # Add stats
        if video.get('view_count', 0) > 0:
            embed.add_field(name="👀 Views", value=f"{video['view_count']:,}", inline=True)
        
        if video.get('like_count', 0) > 0:
            embed.add_field(name="👍 Likes", value=f"{video['like_count']:,}", inline=True)
        
        if video.get('duration'):
            embed.add_field(name="⏱️ Duration", value=video['duration'], inline=True)
        
        embed.set_footer(text="YouTube Monitor Bot")
        
        # Set timestamp
        try:
            published_time = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
            embed.timestamp = published_time
        except:
            pass
        
        return embed
    
    @tasks.loop(minutes=5)  # Check every 5 minutes
    async def monitor_task(self):
        """Monitor YouTube channels for new videos"""
        try:
            logger.info("🔍 Starting monitor check...")
            
            conn = sqlite3.connect(yt_api.db_path)
            cursor = conn.cursor()
            
            # Get all monitored channels
            cursor.execute('''
                SELECT DISTINCT guild_id, notification_channel_id, youtube_channel_id, youtube_channel_name
                FROM monitored_channels
            ''')
            
            channels = cursor.fetchall()
            conn.close()
            
            for guild_id, notif_channel_id, yt_channel_id, yt_channel_name in channels:
                try:
                    # Get Discord channel
                    channel = self.bot.get_channel(int(notif_channel_id))
                    if not channel:
                        continue
                    
                    # Get latest videos
                    videos = yt_api.get_latest_videos(yt_channel_id, 3)
                    if not videos:
                        continue
                    
                    conn = sqlite3.connect(yt_api.db_path)
                    cursor = conn.cursor()
                    
                    # Check last processed video
                    cursor.execute('''
                        SELECT last_video_id FROM last_check 
                        WHERE channel_id = ? AND guild_id = ?
                    ''', (yt_channel_id, guild_id))
                    
                    result = cursor.fetchone()
                    last_video_id = result[0] if result else None
                    
                    new_videos = []
                    
                    # Find new videos
                    for video in videos:
                        # Stop when we reach the last processed video
                        if video['video_id'] == last_video_id:
                            break
                        
                        # Check if already sent
                        cursor.execute('''
                            SELECT video_id FROM sent_notifications 
                            WHERE video_id = ? AND guild_id = ?
                        ''', (video['video_id'], guild_id))
                        
                        if not cursor.fetchone():
                            # For first run, only notify recent videos (last 4 hours)
                            if not last_video_id:
                                try:
                                    pub_time = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                                    if pub_time < datetime.now(timezone.utc) - timedelta(hours=4):
                                        continue
                                except:
                                    continue
                            
                            new_videos.append(video)
                    
                    # Send notifications (oldest first)
                    new_videos.reverse()
                    
                    for video in new_videos:
                        try:
                            embed = self.create_video_embed(video)
                            content = "🔴 **LIVE STREAM STARTED!**" if video.get('is_live') else ""
                            
                            await channel.send(content=content, embed=embed)
                            
                            # Mark as sent
                            cursor.execute('''
                                INSERT OR REPLACE INTO sent_notifications 
                                (video_id, guild_id, sent_at) VALUES (?, ?, ?)
                            ''', (video['video_id'], guild_id, datetime.now().isoformat()))
                            
                            logger.info(f"✅ Sent: {video['title']} to {channel.guild.name}")
                            await asyncio.sleep(2)  # Rate limit
                            
                        except Exception as e:
                            logger.error(f"Error sending notification: {e}")
                    
                    # Update last check
                    if videos:
                        cursor.execute('''
                            INSERT OR REPLACE INTO last_check 
                            (channel_id, guild_id, last_video_id, last_check_time) 
                            VALUES (?, ?, ?, ?)
                        ''', (yt_channel_id, guild_id, videos[0]['video_id'], datetime.now().isoformat()))
                    
                    conn.commit()
                    conn.close()
                    
                    await asyncio.sleep(1)  # Delay between channels
                    
                except Exception as e:
                    logger.error(f"Error monitoring {yt_channel_name}: {e}")
            
            logger.info("✅ Monitor check completed")
            
        except Exception as e:
            logger.error(f"Error in monitor task: {e}")
    
    @monitor_task.before_loop
    async def before_monitor_task(self):
        await self.bot.wait_until_ready()

class YouTubeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(command_prefix='!', intents=intents, help_command=None)
    
    async def setup_hook(self):
        await self.add_cog(YouTubeCog(self))
        
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} commands")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        logger.info(f'{self.user} connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guilds')
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="YouTube channels 📺"
            )
        )

def main():
    # Get tokens from environment variables
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
    
    if not BOT_TOKEN:
        print("❌ Please set BOT_TOKEN environment variable")
        return
    
    if not YOUTUBE_API_KEY:
        print("❌ Please set YOUTUBE_API_KEY environment variable")
        return
    
    # Initialize YouTube API
    global yt_api
    yt_api = YouTubeAPI(YOUTUBE_API_KEY)
    
    # Run bot
    bot = YouTubeBot()
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        logger.error(f"Error running bot: {e}")

if __name__ == "__main__":
    main()