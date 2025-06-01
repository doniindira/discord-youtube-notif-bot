# YouTube Discord Bot - Docker Setup

Bot Discord untuk monitoring channel YouTube dan mengirim notifikasi otomatis ke Discord channel.

## 🚀 Quick Start

### 1. Clone atau Download Files

Pastikan Anda memiliki file-file berikut di server VPS:
```
youtube-discord-bot/
├── Dockerfile
├── docker-compose.yml
├── main.py
├── requirements.txt
├── .env.example
├── .dockerignore
└── README.md
```

### 2. Setup Discord Bot Token

1. Buat file `.env` dari template:
```bash
cp .env.example .env
```

2. Edit file `.env` dan masukkan bot token:
```bash
nano .env
```

3. Ganti `your_discord_bot_token_here` dengan token Discord bot Anda

### 3. Cara Mendapatkan Discord Bot Token

1. Buka [Discord Developer Portal](https://discord.com/developers/applications)
2. Klik "New Application" dan beri nama bot
3. Pergi ke section "Bot" di sidebar
4. Klik "Add Bot" atau "Create a Bot"
5. Copy token yang diberikan
6. **PENTING**: Enable "Slash Commands" di OAuth2 → URL Generator
7. Invite bot ke server dengan permissions: "Send Messages", "Use Slash Commands", "Embed Links"

### 4. Deploy dengan Docker Compose

```bash
# Build dan jalankan bot
docker-compose up -d

# Lihat logs
docker-compose logs -f youtube-bot

# Stop bot
docker-compose down

# Restart bot
docker-compose restart youtube-bot
```

### 5. Alternative: Manual Docker Build

```bash
# Build image
docker build -t youtube-discord-bot .

# Run container
docker run -d \
  --name youtube-bot \
  --restart unless-stopped \
  -e BOT_TOKEN=your_discord_bot_token_here \
  -v $(pwd)/data:/app/data \
  youtube-discord-bot
```

## 📁 File Structure

```
project/
├── data/                    # Database storage (created automatically)
│   └── youtube_bot.db      # SQLite database
├── logs/                   # Log files (optional)
├── main.py                 # Bot source code
├── requirements.txt        # Python dependencies
├── Dockerfile             # Docker build instructions
├── docker-compose.yml     # Docker compose configuration
├── .env                   # Environment variables (create from .env.example)
├── .env.example           # Environment template
└── .dockerignore          # Docker ignore file
```

## 🎯 Bot Commands

### Slash Commands:
- `/add_channel <username> [channel]` - Monitor YouTube channel
- `/remove_channel <username>` - Stop monitoring channel
- `/list_channels` - Show monitored channels
- `/test_notification <username>` - Test notification

### Examples:
```
/add_channel @pewdiepie
/add_channel @mrbeast #notifications
/test_notification @pewdiepie
/list_channels
/remove_channel @pewdiepie
```

## ⚙️ Configuration

### Environment Variables (.env file):
```bash
# Required
BOT_TOKEN=your_discord_bot_token_here
YOUTUBE_API_KEY=your_youtube_api_token_here

# Optional
TZ=Asia/Jakarta
```

### Docker Compose Configuration:
- **Restart Policy**: `unless-stopped` - Bot akan restart otomatis jika container crash
- **Health Check**: Memeriksa database setiap 30 detik
- **Persistent Storage**: Database disimpan di folder `./data`
- **Timezone**: Set ke Asia/Jakarta (bisa diubah)

## 🔧 Management Commands

### Docker Compose Commands:
```bash
# Start bot
docker-compose up -d

# View logs (real-time)
docker-compose logs -f youtube-bot

# View logs (last 100 lines)
docker-compose logs --tail=100 youtube-bot

# Stop bot
docker-compose down

# Restart bot
docker-compose restart youtube-bot

# Update bot (after code changes)
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Check bot status
docker-compose ps
```

### Docker Commands (alternative):
```bash
# Check running containers
docker ps

# View bot logs
docker logs -f youtube-discord-bot

# Stop bot
docker stop youtube-discord-bot

# Start bot
docker start youtube-discord-bot

# Remove container (keep data)
docker rm youtube-discord-bot

# Rebuild image
docker build -t youtube-discord-bot . --no-cache
```

## 📊 Monitoring & Troubleshooting

### Check Bot Status:
```bash
# Container status
docker-compose ps

# Real-time logs
docker-compose logs -f youtube-bot

# Resource usage
docker stats youtube-discord-bot
```

### Common Issues:

#### 1. Bot tidak bisa connect ke Discord
**Solution**: Periksa BOT_TOKEN di file `.env`
```bash
# Check environment variable
docker-compose exec youtube-bot env | grep BOT_TOKEN
```

#### 2. Database error
**Solution**: Periksa permissions folder data
```bash
# Fix permissions
sudo chown -R $USER:$USER ./data
chmod 755 ./data
```

#### 3. YouTube channel tidak ditemukan
**Causes**: 
- Username salah atau channel private
- Rate limiting dari YouTube
- Network connectivity issues

#### 4. Bot tidak mengirim notifikasi
**Check**:
- Bot permissions di Discord server
- Channel masih ada/tidak terhapus
- Database connection

### Log Files:
Bot logs tersimpan di Docker container. Untuk persistent logs:
```bash
# Create logs directory
mkdir -p logs

# Update docker-compose.yml to mount logs
# (already included in the compose file)
```

## 🔐 Security Best Practices

1. **Never commit `.env` file** ke repository
2. **Use strong Discord bot token**
3. **Limit bot permissions** hanya yang diperlukan
4. **Regular updates** untuk dependencies
5. **Monitor logs** untuk aktivitas suspicious

## 🚀 Production Deployment

### VPS Requirements:
- **OS**: Ubuntu 20.04+ atau CentOS 8+
- **RAM**: Minimum 512MB (Recommended 1GB+)
- **Storage**: 2GB+ free space
- **Docker**: Version 20.10+
- **Docker Compose**: Version 1.29+

### Install Docker & Docker Compose:
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version
```

### Deployment Steps:
```bash
# 1. Upload files to VPS
scp -r youtube-discord-bot/ user@your-vps-ip:/home/user/

# 2. SSH to VPS
ssh user@your-vps-ip

# 3. Navigate to project
cd youtube-discord-bot/

# 4. Setup environment
cp .env.example .env
nano .env  # Edit BOT_TOKEN

# 5. Deploy
docker-compose up -d

# 6. Verify
docker-compose logs -f youtube-bot
```

### Auto-start on Boot:
```bash
# Enable Docker service
sudo systemctl enable docker

# Create systemd service (optional)
sudo nano /etc/systemd/system/youtube-bot.service
```

Service file content:
```ini
[Unit]
Description=YouTube Discord Bot
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/user/youtube-discord-bot
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
User=user

[Install]
WantedBy=multi-user.target
```

Enable service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable youtube-bot.service
sudo systemctl start youtube-bot.service
```

## 📈 Performance Optimization

### Resource Limits (docker-compose.yml):
```yaml
services:
  youtube-bot:
    # ... other config
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: '0.5'
        reservations:
          memory: 128M
          cpus: '0.25'
```

### Database Optimization:
Bot menggunakan SQLite dengan optimizations:
- Automatic cleanup old notifications (>30 days)
- Efficient indexing
- Connection pooling

## 🔄 Updates & Maintenance

### Update Bot Code:
```bash
# 1. Stop bot
docker-compose down

# 2. Update files (upload new main.py)
# 3. Rebuild and restart
docker-compose build --no-cache
docker-compose up -d
```

### Database Backup:
```bash
# Create backup
docker-compose exec youtube-bot cp /app/data/youtube_bot.db /app/data/backup_$(date +%Y%m%d).db

# Copy backup to host
docker cp youtube-discord-bot:/app/data/backup_$(date +%Y%m%d).db ./
```

### Clean Up:
```bash
# Remove old images
docker image prune -a

# Remove unused volumes
docker volume prune

# System cleanup
docker system prune -a
```

## 📞 Support

Jika mengalami masalah:
1. Check logs: `docker-compose logs -f youtube-bot`
2. Verify bot token dan permissions
3. Check Discord server permissions
4. Restart bot: `docker-compose restart youtube-bot`

## 📝 Features

- ✅ Monitor multiple YouTube channels
- ✅ Real-time notifications untuk video baru
- ✅ Live stream detection
- ✅ Multiple Discord servers support
- ✅ Persistent database storage
- ✅ Auto-restart on failure
- ✅ Docker containerized
- ✅ Easy deployment dan management

---

**Happy monitoring! 🎉**