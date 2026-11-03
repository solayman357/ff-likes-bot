# Firewall Setup for FF Likes API VPS

## UFW (Ubuntu/Debian)

```bash
# Enable UFW
sudo ufw enable

# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP (for Certbot)
sudo ufw allow 80/tcp

# Allow HTTPS
sudo ufw allow 443/tcp

# Allow API port (direct access, optional if using Nginx)
sudo ufw allow 5000/tcp

# Check status
sudo ufw status
```

## Firewalld (CentOS/RHEL)

```bash
# Enable firewalld
sudo systemctl enable firewalld
sudo systemctl start firewalld

# Allow ports
sudo firewall-cmd --permanent --add-port=22/tcp
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --permanent --add-port=5000/tcp

# Reload
sudo firewall-cmd --reload

# Check
sudo firewall-cmd --list-all
```
