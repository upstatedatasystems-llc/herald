# Gmail Integration Setup Guide

## Overview

Herald uses a dedicated Gmail account to receive intake emails and reply with completed podcast links.

## OAuth2 Configuration Steps

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project named `Herald Automation`.
3. Enable the **Gmail API** under **APIs & Services** -> **Library**.
4. Configure the **OAuth consent screen**:
   - User Type: External or Internal
   - App name: `Herald`
   - Scopes: `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.send`
5. Create Credentials -> **OAuth client ID**:
   - Application type: Web application
   - Authorized redirect URIs: `https://oauth.n8n.io/oauth2/callback` (or your self-hosted n8n OAuth callback URL).
6. Copy Client ID and Client Secret into n8n's Gmail OAuth2 credential settings.
