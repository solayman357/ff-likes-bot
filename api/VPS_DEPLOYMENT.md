# VPS Deployment Guide — FF Likes API

## Prerequisites
- Ubuntu 22.04+ VPS with root access
- Domain name pointing to VPS IP (for HTTPS)
- Python 3.8+

---

## Step 1: Copy Project to VPS

```bash
# From your local machine
scp -r /root/OWN-LIKE-BOT/api/ root@your-vps-ip:/root/ff-api/
```

---

## Step 2: Install Python and Dependencies on VPS

```bash
ssh root@your-vps-ip

# Update system
apt update && apt upgrade -y

# Install Python and pip
apt install -y python3 python3-pip python3-venv

# Create virtual environment
cd /root/ff-api
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 3: Configure Environment Variables

```bash
cd /root/ff-api

cat > .env << 'EOF'
API_KEY=Solayman
JWT_API_URLS=https://kawsarxjwt.lovable.app/api/public/token
PORT=5000
EOF

chmod 600 .env
```

---

## Step 4: Install systemd Service

```bash
# Copy service file
sudo cp /root/ff-api/ff-likes-api.service /etc/systemd/system/ff-likes-api.service

# Reload systemd
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable ff-likes-api
sudo systemctl start ff-likes-api

# Check status
sudo systemctl status ff-likes-api

# View logs
sudo journalctl -u ff-likes-api -f
```

---

## Step 5: Configure Firewall

```bash
# UFW (Ubuntu)
sudo ufw enable
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 5000/tcp
sudo ufw status
```

---

## Step 6: Set Up HTTPS with Nginx + Certbot

```bash
# Install Nginx and Certbot
apt install -y nginx certbot python3-certbot-nginx

# Copy Nginx config
sudo cp /root/ff-api/nginx-ff-likes-api.conf /etc/nginx/sites-available/ff-likes-api
sudo ln -s /etc/nginx/sites-available/ff-likes-api /etc/nginx/sites-enabled/

# Remove default site
sudo rm -f /etc/nginx/sites-enabled/default

# Test Nginx config
sudo nginx -t

# Reload Nginx
sudo systemctl reload nginx

# Obtain SSL certificate (replace with your domain)
sudo certbot --nginx -d your-api-domain.com

# Test auto-renewal
sudo certbot renew --dry-run
```

---

## Step 7: Test the VPS API

```bash
# Health check
curl https://your-api-domain.com/health

# Remaining likes
curl https://your-api-domain.com/remain

# Like test (may return 503 if Garena blocks this IP)
curl "https://your-api-domain.com/like?uid=5933029554&region=BD&key=Solayman"
```

---

## Step 8: Update Bot's API_URL on Render

1. Go to https://dashboard.render.com
2. Select your bot service `ff-likes-bot-1`
3. Environment Variables → Edit `API_URL`
4. Change to `https://your-api-domain.com`
5. Redeploy bot service

---

## Step 9: Test on Telegram

Send to `@Zenclaimv15bot`:
```
/like BD 5933029554
```

---

## Important Notes

- **Garena 503**: If Garena blocks the VPS IP, `/like` will still return 503. Try a different VPS provider if needed.
- **JWT tokens**: The `kawsarxjwt.lovable.app` API generates tokens, but Garena may reject them if the IP is blocked.
- **Account credentials**: 472 real accounts are in `api/account_bd.txt` and `api/account_ind.txt`. Keep these secure.
- **Never commit secrets**: The `.env` file contains secrets. Never push it to GitHub.
