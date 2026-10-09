#!/bin/bash
set -e

echo "Starting CLAIMCHECK Deployment..."

# 1. Update system and clone your latest code
sudo dnf update -y
sudo dnf install git postgresql15-server nginx python3.11 python3.11-pip nodejs -y
cd ~
rm -rf claimcheck-prod
git clone https://github.com/Sohamgaonkhadkar/claimcheck.git claimcheck-prod
cd claimcheck-prod

# 2. Start Database
sudo postgresql-setup --initdb || true
sudo sed -i 's/ident/scram-sha-256/g' /var/lib/pgsql/data/pg_hba.conf
sudo systemctl enable postgresql
sudo systemctl restart postgresql

sudo -u postgres psql -c "CREATE DATABASE claimcheck;" || true
sudo -u postgres psql -c "CREATE USER claimcheck_dev WITH PASSWORD 'claimcheck_dev_only';" || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE claimcheck TO claimcheck_dev;" || true
sudo -u postgres psql -c "ALTER DATABASE claimcheck OWNER TO claimcheck_dev;" || true

# 3. Setup Python Backend
pip3.11 install --user -e .
pip3.11 install --user uvicorn alembic psycopg

export DATABASE_URL="postgresql://claimcheck_dev:claimcheck_dev_only@127.0.0.1:5432/claimcheck"
export CLAIMCHECK_ENV="development"
~/.local/bin/alembic upgrade head

# 4. Create Background Services
sudo bash -c 'cat > /etc/systemd/system/claimcheck-api.service << "EOF"
[Unit]
Description=Claimcheck API
After=network.target postgresql.service
[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/claimcheck-prod
Environment="DATABASE_URL=postgresql://claimcheck_dev:claimcheck_dev_only@127.0.0.1:5432/claimcheck"
Environment="CLAIMCHECK_ENV=development"
ExecStart=/home/ec2-user/.local/bin/uvicorn claimcheck.api.app:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF'

sudo bash -c 'cat > /etc/systemd/system/claimcheck-worker.service << "EOF"
[Unit]
Description=Claimcheck Worker
After=network.target postgresql.service
[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/claimcheck-prod
Environment="DATABASE_URL=postgresql://claimcheck_dev:claimcheck_dev_only@127.0.0.1:5432/claimcheck"
Environment="CLAIMCHECK_ENV=development"
ExecStart=/home/ec2-user/.local/bin/claimcheck-worker
Restart=always
[Install]
WantedBy=multi-user.target
EOF'

sudo mkdir -p /var/lib/claimcheck/private-storage
sudo chown -R ec2-user:ec2-user /var/lib/claimcheck/private-storage

sudo systemctl daemon-reload
sudo systemctl enable claimcheck-api claimcheck-worker
sudo systemctl restart claimcheck-api claimcheck-worker

# 5. Build UI & Configure Nginx
cd ~/claimcheck-prod/web
npm install
npm run build

sudo bash -c 'cat > /etc/nginx/conf.d/claimcheck.conf << "EOF"
server {
    listen 80;
    server_name _;
    root /home/ec2-user/claimcheck-prod/web/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/v1/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF'

# Give Nginx permission to read the files
chmod +x /home/ec2-user
chmod +x /home/ec2-user/claimcheck-prod
chmod +x /home/ec2-user/claimcheck-prod/web

sudo tee /etc/nginx/nginx.conf > /dev/null << "EOF"
user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';
    access_log  /var/log/nginx/access.log  main;
    sendfile        on;
    keepalive_timeout  65;
    include /etc/nginx/conf.d/*.conf;
}
EOF
sudo systemctl enable nginx
sudo systemctl restart nginx

echo ""
echo "✅ DEPLOYMENT COMPLETE! Open http://65.2.161.137 in your browser."
