import discord
from discord.ext import commands, tasks
import requests
import sqlite3
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import List, Dict, Optional
import asyncio
import json
import os
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import aiohttp

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class YouTubeMonitor:
    def __init__(self, api_key: str):
        # Initialize database
        self.db_path = '/app/data/youtube_bot.db' if os.path.exists('/app/data') else 'youtube_bot.db'
        self.init_database()
        
        # YouTube API setup
        self.api_key = api_key
        self.youtube = build('youtube', 'v3', developerKey=api_key)
        
        # Request headers for fallback scraping
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        # Rate limiting
        self.last_api_call = datetime.now()
        self.api_calls_count = 0
        self.api_calls_reset_time = datetime.now()
        self.max_api_calls_per_hour = 3600  # Conservative limit
    
    def init_database(self):
        """Initialize SQLite database"""
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Table for sent notifications
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_notifications (
                video_id TEXT,
                channel_id TEXT,
                title TEXT,
                published_at TEXT,
                notification_sent_at TEXT,
                guild_id TEXT,
                PRIMARY KEY (video_id, guild_id)
            )
        ''')
        
        # Table for channel cache
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_cache (
                channel_username TEXT PRIMARY KEY,
                channel_id TEXT,
                channel_name TEXT,
                subscriber_count INTEGER,
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
                added_at TEXT,
                UNIQUE(guild_id, notification_channel_id, youtube_channel_id)
            )
        ''')
        
        # Table to track last check time per channel
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channel_last_check (
                channel_id TEXT,
                guild_id TEXT,
                last_check_at TEXT,
                last_video_id TEXT,
                last_video_published_at TEXT,
                PRIMARY KEY (channel_id, guild_id)
            )
        ''')
        
        # Table for video cache to reduce API calls
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS video_cache (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                published_at TEXT,
                channel_id TEXT,
                channel_title TEXT,
                thumbnail_url TEXT,
                view_count INTEGER,
                like_count INTEGER,
                duration TEXT,
                is_live BOOLEAN,
                cached_at TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def check_rate_limit(self) -> bool:
        """Check if we can make an API call without exceeding rate limits"""
        now = datetime.now()
        
        # Reset counter every hour
        if now - self.api_calls_reset_time > timedelta(hours=1):
            self.api_calls_count = 0
            self.api_calls_reset_time = now
        
        # Check if we're under the limit
        if self.api_calls_count >= self.max_api_calls_per_hour:
            return False
        
        # Ensure minimum 1 second between calls
        if now - self.last_api_call < timedelta(seconds=1):
            return False
        
        return True
    
    def increment_api_call(self):
        """Increment API call counter"""
        self.api_calls_count += 1
        self.last_api_call = datetime.now()
    
    def get_channel_info_by_username(self, username: str) -> Optional[Dict[str, str]]:
        """Get channel info using YouTube API by username/handle"""
        if not self.check_rate_limit():
            logger.warning("Rate limit reached, using cache or fallback")
            return self.get_channel_info_fallback(username)
        
        # Clean username
        username = username.replace('@', '').strip()
        
        # Check cache first
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT channel_id, channel_name, subscriber_count FROM channel_cache 
            WHERE channel_username = ? AND 
            datetime(cached_at) > datetime('now', '-24 hours')
        ''', (username,))
        
        cached = cursor.fetchone()
        conn.close()
        
        if cached:
            return {
                'channel_id': cached[0], 
                'channel_name': cached[1],
                'subscriber_count': cached[2]
            }
        
        try:
            self.increment_api_call()
            
            # Try different search methods
            search_queries = [f"@{username}", username]
            
            for query in search_queries:
                try:
                    # Search for channels
                    search_response = self.youtube.search().list(
                        q=query,
                        part='snippet',
                        type='channel',
                        maxResults=5
                    ).execute()
                    
                    for item in search_response['items']:
                        channel_id = item['id']['channelId']
                        channel_title = item['snippet']['title']
                        
                        # Get detailed channel info
                        if self.check_rate_limit():
                            self.increment_api_call()
                            channel_response = self.youtube.channels().list(
                                part='snippet,statistics',
                                id=channel_id
                            ).execute()
                            
                            if channel_response['items']:
                                channel_data = channel_response['items'][0]
                                custom_url = channel_data['snippet'].get('customUrl', '').lower()
                                
                                # Check if this matches our search
                                if (custom_url == username.lower() or 
                                    custom_url == f"@{username.lower()}" or
                                    channel_title.lower() == username.lower()):
                                    
                                    result = {
                                        'channel_id': channel_id,
                                        'channel_name': channel_title,
                                        'subscriber_count': int(channel_data['statistics'].get('subscriberCount', 0))
                                    }
                                    
                                    # Cache the result
                                    self.cache_channel_info(username, result)
                                    return result
                
                except HttpError as e:
                    logger.error(f"YouTube API error for {query}: {e}")
                    continue
            
            # If API search fails, try direct channel ID approach
            if len(username) == 24 and username.startswith('UC'):
                return self.get_channel_info_by_id(username)
            
            # Fallback to scraping
            return self.get_channel_info_fallback(username)
            
        except Exception as e:
            logger.error(f"Error getting channel info for {username}: {e}")
            return self.get_channel_info_fallback(username)
    
    def get_channel_info_by_id(self, channel_id: str) -> Optional[Dict[str, str]]:
        """Get channel info using YouTube API by channel ID"""
        if not self.check_rate_limit():
            return None
        
        try:
            self.increment_api_call()
            response = self.youtube.channels().list(
                part='snippet,statistics',
                id=channel_id
            ).execute()
            
            if response['items']:
                channel_data = response['items'][0]
                return {
                    'channel_id': channel_id,
                    'channel_name': channel_data['snippet']['title'],
                    'subscriber_count': int(channel_data['statistics'].get('subscriberCount', 0))
                }
        
        except Exception as e:
            logger.error(f"Error getting channel info by ID {channel_id}: {e}")
        
        return None
    
    def get_channel_info_fallback(self, username: str) -> Optional[Dict[str, str]]:
        """Fallback method using web scraping"""
        username = username.replace('@', '').strip()
        
        try:
            urls_to_try = [
                f"https://www.youtube.com/@{username}",
                f"https://www.youtube.com/c/{username}",
                f"https://www.youtube.com/user/{username}"
            ]
            
            for url in urls_to_try:
                try:
                    response = requests.get(url, headers=self.headers, timeout=10)
                    if response.status_code == 200:
                        content = response.text
                        
                        # Extract channel ID
                        channel_id_patterns = [
                            r'"channelId":"([^"]+)"',
                            r'"externalId":"([^"]+)"',
                            r'channel/([a-zA-Z0-9_-]{24})'
                        ]
                        
                        channel_id = None
                        for pattern in channel_id_patterns:
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
                            result = {
                                'channel_id': channel_id,
                                'channel_name': channel_name,
                                'subscriber_count': 0
                            }
                            self.cache_channel_info(username, result)
                            return result
                
                except requests.exceptions.RequestException:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"Error in fallback channel info: {e}")
            return None
    
    def cache_channel_info(self, username: str, info: Dict):
        """Cache channel information"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO channel_cache 
                (channel_username, channel_id, channel_name, subscriber_count, cached_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (username, info['channel_id'], info['channel_name'], 
                  info.get('subscriber_count', 0), datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error caching channel info: {e}")
    
    def get_latest_videos(self, channel_id: str, max_results: int = 10) -> List[Dict]:
        """Get latest videos using YouTube API"""
        if not self.check_rate_limit():
            logger.warning("Rate limit reached, falling back to RSS")
            return self.get_latest_videos_rss_fallback(channel_id, max_results)
        
        try:
            self.increment_api_call()
            
            # Get channel uploads playlist
            channel_response = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            ).execute()
            
            if not channel_response['items']:
                return []
            
            uploads_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            
            if self.check_rate_limit():
                self.increment_api_call()
                
                # Get videos from uploads playlist
                playlist_response = self.youtube.playlistItems().list(
                    part='snippet',
                    playlistId=uploads_playlist_id,
                    maxResults=max_results,
                    order='date'
                ).execute()
                
                videos = []
                video_ids = []
                
                for item in playlist_response['items']:
                    video_id = item['snippet']['resourceId']['videoId']
                    video_ids.append(video_id)
                    
                    video_info = {
                        'video_id': video_id,
                        'title': item['snippet']['title'],
                        'description': item['snippet']['description'][:500] + '...' if len(item['snippet']['description']) > 500 else item['snippet']['description'],
                        'published_at': item['snippet']['publishedAt'],
                        'channel_title': item['snippet']['channelTitle'],
                        'channel_id': channel_id,
                        'url': f"https://www.youtube.com/watch?v={video_id}",
                        'thumbnail': item['snippet']['thumbnails'].get('maxres', 
                                   item['snippet']['thumbnails'].get('high',
                                   item['snippet']['thumbnails'].get('medium', 
                                   item['snippet']['thumbnails'].get('default', {})))).get('url', ''),
                        'is_live': False,
                        'view_count': 0,
                        'like_count': 0,
                        'duration': ''
                    }
                    videos.append(video_info)
                
                # Get additional video details if we have API quota
                if video_ids and self.check_rate_limit():
                    self.increment_api_call()
                    video_details = self.youtube.videos().list(
                        part='statistics,contentDetails,liveStreamingDetails',
                        id=','.join(video_ids)
                    ).execute()
                    
                    # Update video info with details
                    for i, detail in enumerate(video_details['items']):
                        if i < len(videos):
                            videos[i]['view_count'] = int(detail['statistics'].get('viewCount', 0))
                            videos[i]['like_count'] = int(detail['statistics'].get('likeCount', 0))
                            videos[i]['duration'] = detail['contentDetails']['duration']
                            
                            # Check if live
                            live_details = detail.get('liveStreamingDetails', {})
                            videos[i]['is_live'] = (
                                'actualStartTime' in live_details and 
                                'actualEndTime' not in live_details
                            )
                
                # Cache videos
                self.cache_videos(videos)
                return videos
        
        except Exception as e:
            logger.error(f"Error getting latest videos via API: {e}")
            return self.get_latest_videos_rss_fallback(channel_id, max_results)
    
    def get_latest_videos_rss_fallback(self, channel_id: str, max_results: int = 10) -> List[Dict]:
        """Fallback RSS method for getting videos"""
        import feedparser
        
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        
        try:
            response = requests.get(rss_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            feed = feedparser.parse(response.content)
            
            videos = []
            for entry in feed.entries[:max_results]:
                video_id = entry.link.split('watch?v=')[-1]
                
                try:
                    if hasattr(entry, 'published_parsed') and entry.published_parsed:
                        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
                    else:
                        published_at = datetime.strptime(entry.published, '%Y-%m-%dT%H:%M:%S%z').isoformat()
                except:
                    published_at = datetime.now(timezone.utc).isoformat()
                
                video_info = {
                    'video_id': video_id,
                    'title': entry.title,
                    'description': getattr(entry, 'summary', '')[:500] + '...' if len(getattr(entry, 'summary', '')) > 500 else getattr(entry, 'summary', ''),
                    'published_at': published_at,
                    'channel_title': entry.author,
                    'channel_id': channel_id,
                    'url': entry.link,
                    'thumbnail': f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                    'is_live': False,
                    'view_count': 0,
                    'like_count': 0,
                    'duration': ''
                }
                videos.append(video_info)
            
            return videos
            
        except Exception as e:
            logger.error(f"Error fetching RSS feed for {channel_id}: {e}")
            return []
    
    def cache_videos(self, videos: List[Dict]):
        """Cache video information"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            for video in videos:
                cursor.execute('''
                    INSERT OR REPLACE INTO video_cache 
                    (video_id, title, description, published_at, channel_id, 
                     channel_title, thumbnail_url, view_count, like_count, 
                     duration, is_live, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    video['video_id'], video['title'], video['description'],
                    video['published_at'], video['channel_id'], video['channel_title'],
                    video['thumbnail'], video['view_count'], video['like_count'],
                    video['duration'], video['is_live'], datetime.now().isoformat()
                ))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error caching videos: {e}")
    
    def is_video_live(self, video_id: str) -> bool:
        """Check if video is currently live using API"""
        if not self.check_rate_limit():
            return self.check_if_live_stream_fallback(video_id)
        
        try:
            self.increment_api_call()
            response = self.youtube.videos().list(
                part='liveStreamingDetails',
                id=video_id
            ).execute()
            
            if response['items']:
                live_details = response['items'][0].get('liveStreamingDetails', {})
                return (
                    'actualStartTime' in live_details and 
                    'actualEndTime' not in live_details
                )
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking live status via API: {e}")
            return self.check_if_live_stream_fallback(video_id)
    
    def check_if_live_stream_fallback(self, video_id: str) -> bool:
        """Fallback method to check if video is live"""
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code == 200:
                content = response.text.lower()
                live_indicators = [
                    '"islivebroadcast":true',
                    '"islive":true',
                    'live now',
                    '"islivecontent":true',
                    '"livebroadcastcontent":"live"'
                ]
                return any(indicator in content for indicator in live_indicators)
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking live status fallback for {video_id}: {e}")
            return False

# Initialize YouTube Monitor (will be set in main())
yt_monitor = None

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
        channel_info = yt_monitor.get_channel_info_by_username(channel_username)
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
        
        try:
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
            
            # Initialize last check tracking
            cursor.execute('''
                INSERT OR REPLACE INTO channel_last_check 
                (channel_id, guild_id, last_check_at, last_video_id, last_video_published_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                channel_info['channel_id'],
                str(interaction.guild.id),
                datetime.now().isoformat(),
                "",
                ""
            ))
            
            conn.commit()
            
            embed = discord.Embed(
                title="✅ Channel Berhasil Ditambahkan!",
                description=f"**{channel_info['channel_name']}** akan dimonitor di {notification_channel.mention}",
                color=0x00ff00
            )
            embed.add_field(name="Channel ID", value=channel_info['channel_id'], inline=True)
            embed.add_field(name="Username", value=channel_username, inline=True)
            if channel_info.get('subscriber_count'):
                embed.add_field(name="Subscribers", value=f"{channel_info['subscriber_count']:,}", inline=True)
            
            await interaction.followup.send(embed=embed)
            
        except sqlite3.IntegrityError:
            await interaction.followup.send(f"⚠️ Channel **{channel_info['channel_name']}** sudah dimonitor di {notification_channel.mention}")
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            await interaction.followup.send("❌ Terjadi error saat menambahkan channel. Silakan coba lagi.")
        finally:
            conn.close()
    
    @discord.app_commands.command(name="remove_channel", description="Hapus channel YouTube dari monitoring")
    @discord.app_commands.describe(channel_username="Username channel YouTube yang ingin dihapus")
    async def remove_channel(self, interaction: discord.Interaction, channel_username: str):
        conn = sqlite3.connect(yt_monitor.db_path)
        cursor = conn.cursor()
        
        # Get channel info before deleting
        cursor.execute('''
            SELECT youtube_channel_id, youtube_channel_name FROM monitored_channels 
            WHERE guild_id = ? AND (youtube_channel_username = ? OR youtube_channel_name = ?)
        ''', (str(interaction.guild.id), channel_username, channel_username))
        
        channel_info = cursor.fetchone()
        
        if not channel_info:
            conn.close()
            await interaction.response.send_message(f"❌ Channel `{channel_username}` tidak ditemukan dalam daftar monitoring!")
            return
        
        # Delete monitored channel
        cursor.execute('''
            DELETE FROM monitored_channels 
            WHERE guild_id = ? AND (youtube_channel_username = ? OR youtube_channel_name = ?)
        ''', (str(interaction.guild.id), channel_username, channel_username))
        
        # Delete last check tracking
        cursor.execute('''
            DELETE FROM channel_last_check 
            WHERE guild_id = ? AND channel_id = ?
        ''', (str(interaction.guild.id), channel_info[0]))
        
        conn.commit()
        conn.close()
        
        await interaction.response.send_message(f"✅ Channel **{channel_info[1]}** berhasil dihapus dari monitoring!")
    
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
        
        for i, (name, username, channel_id, added_at) in enumerate(channels[:25], 1):  # Limit to 25 for embed limits
            channel = self.bot.get_channel(int(channel_id))
            channel_mention = channel.mention if channel else "Channel Terhapus"
            
            embed.add_field(
                name=f"{i}. {name}",
                value=f"Username: `{username}`\nNotifikasi: {channel_mention}",
                inline=False
            )
        
        if len(channels) > 25:
            embed.set_footer(text=f"Menampilkan 25 dari {len(channels)} channel")
        
        await interaction.response.send_message(embed=embed)
    
    @discord.app_commands.command(name="test_notification", description="Test notifikasi dengan video terbaru dari channel")
    @discord.app_commands.describe(channel_username="Username channel YouTube untuk test")
    async def test_notification(self, interaction: discord.Interaction, channel_username: str):
        await interaction.response.defer()
        
        # Get channel info
        channel_info = yt_monitor.get_channel_info_by_username(channel_username)
        if not channel_info:
            await interaction.followup.send(f"❌ Channel `{channel_username}` tidak ditemukan!")
            return
        
        # Get latest video
        videos = yt_monitor.get_latest_videos(channel_info['channel_id'], 1)
        
        if not videos:
            await interaction.followup.send("❌ Tidak dapat mengambil video dari channel tersebut!")
            return
        
        video = videos[0]
        
        # Check if live
        if not video.get('is_live'):
            video['is_live'] = yt_monitor.is_video_live(video['video_id'])
        
        # Send test notification
        embed = self.create_video_embed(video, video['is_live'], is_test=True)
        await interaction.followup.send("🧪 **TEST NOTIFICATION**", embed=embed)
    
    @discord.app_commands.command(name="api_status", description="Cek status API dan quota usage")
    async def api_status(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔍 API Status",
            color=0x00ff00
        )
        
        embed.add_field(
            name="API Calls This Hour",
            value=f"{yt_monitor.api_calls_count}/{yt_monitor.max_api_calls_per_hour}",
            inline=True
        )
        
        embed.add_field(
            name="Rate Limited",
            value="❌ Yes" if not yt_monitor.check_rate_limit() else "✅ No",
            inline=True
        )
        
        embed.add_field(
            name="Monitor Task",
            value="✅ Running" if self.monitor_task.is_running() else "❌ Stopped",
            inline=True
        )
        
        # Get monitoring stats
        conn = sqlite3.connect(yt_monitor.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT COUNT(*) FROM monitored_channels WHERE guild_id = ?
        ''', (str(interaction.guild.id),))
        
        channel_count = cursor.fetchone()[0]
        conn.close()
        
        embed.add_field(
            name="Monitored Channels",
            value=str(channel_count),
            inline=True
        )
        
        await interaction.response.send_message(embed=embed)
    
    def create_video_embed(self, video_info: Dict, is_live: bool = False, is_test: bool = False) -> discord.Embed:
        """Create Discord embed for video notification"""
        title_prefix = "🔴 LIVE" if is_live else "📹 VIDEO BARU"
        if is_test:
            title_prefix = "🧪 TEST - " + title_prefix
        
        embed = discord.Embed(
            title=video_info['title'][:256] if video_info['title'] else "No Title",
            url=video_info['url'],
            description=video_info.get('description', 'Tidak ada deskripsi')[:2048],
            color=0xff0000 if is_live else 0x00ff00
        )
        
        embed.set_author(
            name=f"{title_prefix} - {video_info.get('channel_title', 'Unknown Channel')}",
            icon_url="https://www.youtube.com/favicon.ico"
        )
        
        if video_info.get('thumbnail'):
            embed.set_image(url=video_info['thumbnail'])
        
        # Add video stats if available
        if video_info.get('view_count', 0) > 0:
            embed.add_field(
                name="👀 Views",
                value=f"{video_info['view_count']:,}",
                inline=True
            )
        
        if video_info.get('like_count', 0) > 0:
            embed.add_field(
                name="👍 Likes",
                value=f"{video_info['like_count']:,}",
                inline=True
            )
        
        if video_info.get('duration') and video_info['duration'] != '':
            # Parse ISO 8601 duration (PT4M13S -> 4:13)
            duration = video_info['duration']
            if duration.startswith('PT'):
                import re
                match = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
                if match:
                    hours, minutes, seconds = match.groups()
                    duration_str = ""
                    if hours:
                        duration_str += f"{hours}:"
                    if minutes:
                        duration_str += f"{minutes:0>2}:" if hours else f"{minutes}:"
                    else:
                        duration_str += "0:"
                    if seconds:
                        duration_str += f"{seconds:0>2}"
                    else:
                        duration_str += "00"
                    
                    embed.add_field(
                        name="⏱️ Duration",
                        value=duration_str,
                        inline=True
                    )
        
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
    
    @tasks.loop(minutes=3)  # Check every 3 minutes
    async def monitor_task(self):
        """Background task to monitor YouTube channels"""
        try:
            logger.info("🔍 Starting monitor task...")
            
            conn = sqlite3.connect(yt_monitor.db_path)
            cursor = conn.cursor()
            
            # Get all monitored channels
            cursor.execute('''
                SELECT DISTINCT guild_id, notification_channel_id, youtube_channel_id, 
                       youtube_channel_name, youtube_channel_username
                FROM monitored_channels
            ''')
            
            monitored = cursor.fetchall()
            conn.close()
            
            logger.info(f"Found {len(monitored)} channels to monitor")
            
            for guild_id, notif_channel_id, yt_channel_id, yt_channel_name, yt_username in monitored:
                try:
                    logger.info(f"Checking channel: {yt_channel_name} for guild: {guild_id}")
                    
                    # Get Discord channel
                    channel = self.bot.get_channel(int(notif_channel_id))
                    if not channel:
                        logger.warning(f"Discord channel {notif_channel_id} not found")
                        continue
                    
                    # Get last check info
                    conn = sqlite3.connect(yt_monitor.db_path)
                    cursor = conn.cursor()
                    
                    cursor.execute('''
                        SELECT last_video_id, last_check_at, last_video_published_at 
                        FROM channel_last_check 
                        WHERE channel_id = ? AND guild_id = ?
                    ''', (yt_channel_id, guild_id))
                    
                    last_check_info = cursor.fetchone()
                    last_video_id = last_check_info[0] if last_check_info else ""
                    last_video_published = last_check_info[2] if last_check_info else ""
                    
                    # Get latest videos
                    videos = yt_monitor.get_latest_videos(yt_channel_id, 5)
                    
                    if not videos:
                        logger.warning(f"No videos found for {yt_channel_name}")
                        conn.close()
                        continue
                    
                    new_videos = []
                    
                    # Check for new videos
                    for video in videos:
                        # Skip if already notified
                        cursor.execute('''
                            SELECT video_id FROM sent_notifications 
                            WHERE video_id = ? AND guild_id = ?
                        ''', (video['video_id'], guild_id))
                        
                        if cursor.fetchone():
                            continue
                        
                        # If this is first time running, only notify very recent videos (last 2 hours)
                        if not last_video_id:
                            try:
                                published_time = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                                current_time = datetime.now(timezone.utc)
                                
                                if published_time < current_time - timedelta(hours=2):
                                    continue
                            except:
                                continue
                        else:
                            # If we have last video info, only process newer videos
                            if last_video_published:
                                try:
                                    video_time = datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))
                                    last_time = datetime.fromisoformat(last_video_published.replace('Z', '+00:00'))
                                    
                                    if video_time <= last_time:
                                        continue
                                except:
                                    pass
                        
                        new_videos.append(video)
                    
                    # Send notifications for new videos (oldest first)
                    new_videos.sort(key=lambda x: x['published_at'])
                    
                    for video in new_videos:
                        try:
                            # Check if live (use cached value if available)
                            if not video.get('is_live'):
                                video['is_live'] = yt_monitor.is_video_live(video['video_id'])
                            
                            # Send notification
                            embed = self.create_video_embed(video, video['is_live'])
                            
                            # Add role mention if it's a live stream
                            content = ""
                            if video['is_live']:
                                content = "🔴 **LIVE STREAM DIMULAI!** 🔴"
                            
                            await channel.send(content=content, embed=embed)
                            
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
                            
                            logger.info(f"✅ Sent notification for: {video['title']} to {channel.guild.name}")
                            
                            # Delay to avoid rate limits
                            await asyncio.sleep(2)
                            
                        except discord.Forbidden:
                            logger.error(f"❌ No permission to send message to {channel.name} in {channel.guild.name}")
                        except Exception as e:
                            logger.error(f"❌ Error sending notification: {e}")
                    
                    # Update last check info
                    if videos:
                        cursor.execute('''
                            INSERT OR REPLACE INTO channel_last_check 
                            (channel_id, guild_id, last_check_at, last_video_id, last_video_published_at)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (
                            yt_channel_id,
                            guild_id,
                            datetime.now().isoformat(),
                            videos[0]['video_id'],
                            videos[0]['published_at']
                        ))
                    
                    conn.commit()
                    conn.close()
                    
                    # Delay between channels to avoid rate limits
                    await asyncio.sleep(5)
                    
                except Exception as e:
                    logger.error(f"❌ Error monitoring channel {yt_channel_name}: {e}")
                    if 'conn' in locals():
                        conn.close()
            
            logger.info("✅ Monitor task completed")
            
        except Exception as e:
            logger.error(f"❌ Error in monitor task: {e}")
    
    @monitor_task.before_loop
    async def before_monitor_task(self):
        await self.bot.wait_until_ready()
        logger.info("🤖 Bot ready, starting monitor task...")

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
    Main function - Configure your bot token and YouTube API key here
    """
    # Get configuration from environment variables
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
    
    if not BOT_TOKEN:
        print("❌ Please set BOT_TOKEN environment variable")
        print("💡 Get it from: https://discord.com/developers/applications")
        print("   1. Create new application")
        print("   2. Go to 'Bot' section")
        print("   3. Create bot and copy token")
        print("   4. Enable 'Slash Commands' in OAuth2 → URL Generator")
        print("   5. Invite bot to your server with required permissions")
        return
    
    if not YOUTUBE_API_KEY:
        print("❌ Please set YOUTUBE_API_KEY environment variable")
        print("💡 Get YouTube API key from: https://console.developers.google.com/")
        print("   1. Create new project or select existing")
        print("   2. Enable YouTube Data API v3")
        print("   3. Create credentials (API Key)")
        print("   4. Restrict API key to YouTube Data API v3")
        print("   5. Set quota limits as needed")
        return
    
    # Initialize global YouTube Monitor
    global yt_monitor
    yt_monitor = YouTubeMonitor(YOUTUBE_API_KEY)
    
    # Initialize and run bot
    bot = YouTubeBot()
    
    try:
        bot.run(BOT_TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid bot token! Please check your BOT_TOKEN")
    except Exception as e:
        logger.error(f"❌ Error running bot: {e}")
        print("❌ Error running bot. Check logs for details.")
    except KeyboardInterrupt:
        print("\n👋 Bot stopped. Goodbye!")

if __name__ == "__main__":
    main()