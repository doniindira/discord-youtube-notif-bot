import discord
from discord.ext import commands, tasks
import requests
import sqlite3
from datetime import datetime, timedelta
import logging
import re
from typing import List, Dict, Optional
import feedparser
import asyncio
import json
import os

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class YouTubeMonitor:
    def __init__(self):
        # Initialize database
        self.db_path = '/app/data/youtube_bot.db' if os.path.exists('/app/data') else 'youtube_bot.db'
        self.init_database()
        
        # Request headers to mimic browser
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
    def init_database(self):
        """Initialize SQLite database"""
        # Ensure directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Table for sent notifications
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_notifications (
                video_id TEXT PRIMARY KEY,
                channel_id TEXT,
                title TEXT,
                published_at TEXT,
                notification_sent_at TEXT,
                guild_id TEXT
            )
        ''')
        
        # Table for channel cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_cache (
                channel_username TEXT PRIMARY KEY,
                channel_id TEXT,
                channel_name TEXT,
                cached_at TEXT
            )
        ''')
        
        # Table for monitored channels per guild
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                notification_channel_id TEXT,
                youtube_channel_username TEXT,
                youtube_channel_id TEXT,
                youtube_channel_name TEXT,
                added_at TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_channel_id_from_username(self, channel_username: str) -> Optional[Dict[str, str]]:
        """Get channel ID and name from username using web scraping"""
        # Check cache first
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT channel_id, channel_name FROM channel_cache 
            WHERE channel_username = ? AND 
            datetime(cached_at) > datetime('now', '-7 days')
        ''', (channel_username,))
        
        cached = cursor.fetchone()
        conn.close()
        
        if cached:
            return {'channel_id': cached[0], 'channel_name': cached[1]}
        
        # Clean username
        username = channel_username.replace('@', '')
        
        try:
            # Try different URL formats
            urls_to_try = [
                f"https://www.youtube.com/@{username}",
                f"https://www.youtube.com/c/{username}",
                f"https://www.youtube.com/user/{username}",
                f"https://www.youtube.com/channel/{username}"
            ]
            
            for url in urls_to_try:
                try:
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        
                        # Look for channel ID
                        patterns = [
                            r'"channelId":"([^"]+)"',
                            r'"externalId":"([^"]+)"',
                            r'channel/([a-zA-Z0-9_-]{24})',
                            r'"browseId":"([^"]+)"'
                        ]
                        
                        channel_id = None
                        for pattern in patterns:
                            match = re.search(pattern, content)
                            if match and len(match.group(1)) == 24 and match.group(1).startswith('UC'):
                                channel_id = match.group(1)
                                break
                        
                        # Extract channel name
                        name_patterns = [
                            r'<meta property="og:title" content="([^"]+)"',
                            r'"channelMetadataRenderer":{"title":"([^"]+)"'
                        ]
                        
                        channel_name = username
                        for pattern in name_patterns:
                            match = re.search(pattern, content)
                            if match:
                                channel_name = match.group(1)
                                break
                        
                        if channel_id:
                            # Cache the result
                            conn = sqlite3.connect(self.db_path)
                            cursor = conn.cursor()
                            cursor.execute('''
                                INSERT OR REPLACE INTO channel_cache 
                                (channel_username, channel_id, channel_name, cached_at)
                                VALUES (?, ?, ?, ?)
                            ''', (channel_username, channel_id, channel_name, datetime.now().isoformat()))
                            conn.commit()
                            conn.close()
                            
                            return {'channel_id': channel_id, 'channel_name': channel_name}
                
                except requests.exceptions.RequestException:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting channel info: {e}")
            return None
    
    def get_latest_videos_from_rss(self, channel_id: str, channel_name: str) -> List[Dict]:
        """Get latest videos from YouTube RSS feed"""
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            response = requests.get(rss_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            feed = feedparser.parse(response.content)
            
            videos = []
            for entry in feed.entries[:5]:  # Get last 5 videos
                video_id = entry.link.split('watch?v=')[-1]
                published_at = datetime.strptime(entry.published, '%Y-%m-%dT%H:%M:%S%z').isoformat()
                
                video_info = {
                    'video_id': video_id,
                    'title': entry.title,
                    'description': getattr(entry, 'summary', '')[:200] + '...' if len(getattr(entry, 'summary', '')) > 200 else getattr(entry, 'summary', ''),
                    'published_at': published_at,
                    'channel_title': channel_name,
                    'channel_id': channel_id,
                    'url': entry.link,
                    'thumbnail': f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                }
                videos.append(video_info)
            
            return videos
            
        except Exception as e:
            logger.error(f"Error fetching RSS feed: {e}")
            return []
    
    def check_if_live_stream(self, video_id: str) -> bool:
        """Check if a video is currently live"""
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                content = response.text.lower()
                live_indicators = [
                    '"islivebroadcast":true',
                    '"islive":true',
                    'live now',
                    '"islivecontent":true'
                ]
                return any(indicator in content for indicator in live_indicators)
            
            return False
            
        except Exception:
            return False

# Initialize YouTube Monitor
yt_monitor = YouTubeMonitor()

class YouTubeBotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.monitor_task.start()
    
    def cog_unload(self):
        self.monitor_task.cancel()
    
    @discord.app_commands.command(name="add_channel", description="Tambah channel YouTube untuk dimonitor")
    @discord.app_commands.describe(
        channel_username="Username channel YouTube (contoh: @pewdiepie atau pewdiepie)",
        notification_channel="Channel Discord untuk notifikasi (opsional, default channel ini)"
    )
    async def add_channel(self, interaction: discord.Interaction, channel_username: str, notification_channel: discord.TextChannel = None):
        await interaction.response.defer()
        
        if notification_channel is None:
            notification_channel = interaction.channel
        
        # Check permissions
        if not notification_channel.permissions_for(interaction.guild.me).send_messages:
            await interaction.followup.send("❌ Bot tidak memiliki permission untuk mengirim pesan ke channel tersebut!")
            return
        
        # Get channel info
        channel_info = yt_monitor.get_channel_id_from_username(channel_username)
        if not channel_info:
            await interaction.followup.send(f"❌ Tidak dapat menemukan channel YouTube: `{channel_username}`")
            return
        
        # Add to database
        conn = sqlite3.connect(yt_monitor.db_path)
        cursor = conn.cursor()
        
        # Check if already exists
        cursor.execute('''
            SELECT id FROM monitored_channels 
            WHERE guild_id = ? AND youtube_channel_id = ? AND notification_channel_id = ?
        ''', (str(interaction.guild.id), channel_info['channel_id'], str(notification_channel.id)))
        
        if cursor.fetchone():
            conn.close()
            await interaction.followup.send(f"⚠️ Channel **{channel_info['channel_name']}** sudah dimonitor di {notification_channel.mention}")
            return
        
        # Add new monitored channel
        cursor.execute('''
            INSERT INTO monitored_channels 
            (guild_id, notification_channel_id, youtube_channel_username, youtube_channel_id, youtube_channel_name, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            str(interaction.guild.id),
            str(notification_channel.id),
            channel_username,
            channel_info['channel_id'],
            channel_info['channel_name'],
            datetime.now().isoformat()
        ))
        
        conn.commit()
        conn.close()
        
        embed = discord.Embed(
            title="✅ Channel Berhasil Ditambahkan!",
            description=f"**{channel_info['channel_name']}** akan dimonitor di {notification_channel.mention}",
            color=0x00ff00
        )
        embed.add_field(name="Channel ID", value=channel_info['channel_id'], inline=True)
        embed.add_field(name="Username", value=channel_username, inline=True)
        
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="remove_channel", description="Hapus channel YouTube dari monitoring")
    @discord.app_commands.describe(channel_username="Username channel YouTube yang ingin dihapus")
    async def remove_channel(self, interaction: discord.Interaction, channel_username: str):
        conn = sqlite3.connect(yt_monitor.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM monitored_channels 
            WHERE guild_id = ? AND youtube_channel_username = ?
        ''', (str(interaction.guild.id), channel_username))
        
        if cursor.rowcount > 0:
            conn.commit()
            conn.close()
            await interaction.response.send_message(f"✅ Channel `{channel_username}` berhasil dihapus dari monitoring!")
        else:
            conn.close()
            await interaction.response.send_message(f"❌ Channel `{channel_username}` tidak ditemukan dalam daftar monitoring!")
    
    @discord.app_commands.command(name="list_channels", description="Lihat daftar channel yang dimonitor")
    async def list_channels(self, interaction: discord.Interaction):
        conn = sqlite3.connect(yt_monitor.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT youtube_channel_name, youtube_channel_username, notification_channel_id, added_at
            FROM monitored_channels 
            WHERE guild_id = ?
            ORDER BY added_at DESC
        ''', (str(interaction.guild.id),))
        
        channels = cursor.fetchall()
        conn.close()
        
        if not channels:
            await interaction.response.send_message("📝 Belum ada channel yang dimonitor di server ini.")
            return
        
        embed = discord.Embed(
            title="📺 Daftar Channel YouTube yang Dimonitor",
            color=0xff0000
        )
        
        for i, (name, username, channel_id, added_at) in enumerate(channels, 1):
            channel = self.bot.get_channel(int(channel_id))
            channel_mention = channel.mention if channel else "Channel Terhapus"
            
            embed.add_field(
                name=f"{i}. {name}",
                value=f"Username: `{username}`\nNotifikasi: {channel_mention}",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="test_notification", description="Test notifikasi dengan video terbaru dari channel")
    @discord.app_commands.describe(channel_username="Username channel YouTube untuk test")
    async def test_notification(self, interaction: discord.Interaction, channel_username: str):
        await interaction.response.defer()
        
        # Get channel info
        channel_info = yt_monitor.get_channel_id_from_username(channel_username)
        if not channel_info:
            await interaction.followup.send(f"❌ Channel `{channel_username}` tidak ditemukan!")
            return
        
        # Get latest video
        videos = yt_monitor.get_latest_videos_from_rss(
            channel_info['channel_id'], 
            channel_info['channel_name']
        )
        
        if not videos:
            await interaction.followup.send("❌ Tidak dapat mengambil video dari channel tersebut!")
            return
        
        video = videos[0]  # Get the latest video
        is_live = yt_monitor.check_if_live_stream(video['video_id'])
        
        # Send test notification
        embed = self.create_video_embed(video, is_live, is_test=True)
        await interaction.followup.send("🧪 **TEST NOTIFICATION**", embed=embed)
    
    def create_video_embed(self, video_info: Dict, is_live: bool = False, is_test: bool = False) -> discord.Embed:
        """Create Discord embed for video notification"""
        title_prefix = "🔴 LIVE" if is_live else "📹 VIDEO BARU"
        if is_test:
            title_prefix = "🧪 TEST - " + title_prefix
        
        embed = discord.Embed(
            title=video_info['title'][:256],
            url=video_info['url'],
            description=video_info['description'][:2048] if video_info['description'] else "Tidak ada deskripsi",
            color=0xff0000 if is_live else 0x00ff00
        )
        
        embed.set_author(
            name=f"{title_prefix} - {video_info['channel_title']}",
            icon_url="https://www.youtube.com/favicon.ico"
        )
        
        embed.set_image(url=video_info['thumbnail'])
        
        embed.set_footer(
            text="YouTube Notification Bot",
            icon_url="https://www.youtube.com/favicon.ico"
        )
        
        # Parse and format timestamp
        try:
            published_time = datetime.fromisoformat(video_info['published_at'].replace('Z', '+00:00'))
            embed.timestamp = published_time
        except:
            pass
        
        return embed
    
    @tasks.loop(minutes=10)  # Check every 10 minutes
    async def monitor_task(self):
        """Background task to monitor YouTube channels"""
        try:
            conn = sqlite3.connect(yt_monitor.db_path)
            cursor = conn.cursor()
            
            # Get all monitored channels
            cursor.execute('''
                SELECT DISTINCT guild_id, notification_channel_id, youtube_channel_id, youtube_channel_name, youtube_channel_username
                FROM monitored_channels
            ''')
            
            monitored = cursor.fetchall()
            conn.close()
            
            for guild_id, notif_channel_id, yt_channel_id, yt_channel_name, yt_username in monitored:
                try:
                    # Get Discord channel
                    channel = self.bot.get_channel(int(notif_channel_id))
                    if not channel:
                        continue
                    
                    # Get latest videos
                    videos = yt_monitor.get_latest_videos_from_rss(yt_channel_id, yt_channel_name)
                    
                    for video in videos:
                        # Check if already notified
                        conn = sqlite3.connect(yt_monitor.db_path)
                        cursor = conn.cursor()
                        
                        cursor.execute('''
                            SELECT video_id FROM sent_notifications 
                            WHERE video_id = ? AND guild_id = ?
                        ''', (video['video_id'], guild_id))
                        
                        if cursor.fetchone():
                            conn.close()
                            continue
                        
                        # Check if video is recent (within last 2 hours)
                        try:
                            published_time = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                            current_time = datetime.now(published_time.tzinfo)
                            
                            if published_time < current_time - timedelta(hours=2):
                                conn.close()
                                continue
                        except:
                            conn.close()
                            continue
                        
                        # Check if live
                        is_live = yt_monitor.check_if_live_stream(video['video_id'])
                        
                        # Send notification
                        embed = self.create_video_embed(video, is_live)
                        
                        try:
                            await channel.send(embed=embed)
                            
                            # Mark as sent
                            cursor.execute('''
                                INSERT OR REPLACE INTO sent_notifications 
                                (video_id, channel_id, title, published_at, notification_sent_at, guild_id)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (
                                video['video_id'],
                                yt_channel_id,
                                video['title'],
                                video['published_at'],
                                datetime.now().isoformat(),
                                guild_id
                            ))
                            
                            conn.commit()
                            logger.info(f"Sent notification for: {video['title']} to {channel.guild.name}")
                            
                        except discord.Forbidden:
                            logger.error(f"No permission to send message to {channel.name} in {channel.guild.name}")
                        except Exception as e:
                            logger.error(f"Error sending notification: {e}")
                        
                        conn.close()
                        
                        # Small delay to avoid rate limits
                        await asyncio.sleep(2)
                    
                    # Delay between channels
                    await asyncio.sleep(5)
                    
                except Exception as e:
                    logger.error(f"Error monitoring channel {yt_channel_name}: {e}")
            
        except Exception as e:
            logger.error(f"Error in monitor task: {e}")
    
    @monitor_task.before_loop
    async def before_monitor_task(self):
        await self.bot.wait_until_ready()

class YouTubeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None
        )
    
    async def setup_hook(self):
        await self.add_cog(YouTubeBotCog(self))
        
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guilds')
        
        # Set bot status
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="YouTube channels 📺"
        )
        await self.change_presence(activity=activity)

def main():
    """
    Main function - Configure your bot token here
    """
    # Get bot token from environment variable
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ Please set BOT_TOKEN environment variable")
        print("💡 Get it from: https://discord.com/developers/applications")
        print("   1. Create new application")
        print("   2. Go to 'Bot' section")
        print("   3. Create bot and copy token")
        print("   4. Enable 'Slash Commands' in OAuth2 → URL Generator")
        print("   5. Invite bot to your server with required permissions")
        return
    
    # Initialize and run bot
    bot = YouTubeBot()
    
    try:
        bot.run(BOT_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid bot token! Please check your BOT_TOKEN")
    except KeyboardInterrupt:
        print("\n👋 Bot stopped. Goodbye!")

if __name__ == "__main__":
    main()