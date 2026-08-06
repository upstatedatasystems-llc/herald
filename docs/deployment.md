# Ubuntu 24.04 ARM64 (OCI Ampere A1) Deployment Guide

This guide provides step-by-step instructions for deploying Herald on an Oracle Cloud Infrastructure (OCI) Ampere A1 instance (`VM.Standard.A1.Flex`, 1 OCPU, 6 GB RAM, Ubuntu 24.04 LTS).

## Step 1: System Packages & Dependencies

Log into your server via SSH and install required packages:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release git ffmpeg python3-pip python3-venv
```

## Step 2: Install Docker Engine & Compose

```bash
# Add Docker's official GPG key:
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add repository:
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable Docker service and add current user to docker group:
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

## Step 3: Create Installation Root Directory

```bash
sudo mkdir -p /opt/herald/data /opt/herald/models/kokoro /opt/herald/backups
sudo chown -R $USER:$USER /opt/herald
```

## Step 4: Clone Repository & Configure Environment

```bash
cd /opt/herald
git clone https://github.com/upstatedatasystems-llc/herald.git repository
cd repository

cp .env.example .env
nano .env
```

Set secure passwords and values in `.env`:
- `POSTGRES_PASSWORD`: Strong random password.
- `GEMINI_API_KEY`: Your Gemini API key.
- `EMAIL_ALLOWED_SENDERS`: Comma-separated list of authorized email addresses.
- `GOOGLE_DRIVE_FOLDER_ID`: Folder ID for episode uploads.

## Step 5: Build & Launch Docker Compose Stack

```bash
make build
make up
```

Verify service containers are running cleanly:

```bash
docker compose ps
```

## Step 6: Run Database Migrations & System Smoke Test

```bash
make migrate
make smoke-test
```

## Step 7: Configure n8n & Google Integrations

1. Access n8n UI locally via SSH tunnel (`ssh -L 5678:localhost:5678 user@your-oci-ip`).
2. Open `http://localhost:5678` in your browser.
3. Import the workflow JSON files from `n8n/workflows/`.
4. Configure Gmail and Google Drive OAuth2 credentials.
5. Activate all imported workflows.

## Step 8: Enable System Auto-Start on Reboot

```bash
sudo systemctl enable docker
```
Docker Compose restart policy `unless-stopped` automatically restarts all Herald containers when the host reboots.
