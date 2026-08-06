# Google Drive Integration Setup Guide

## Overview

Completed MP3 podcast episodes are uploaded into a designated private Google Drive folder named `Herald Episodes`.

## Setup Steps

1. In your Google Drive account, create a new folder named `Herald Episodes`.
2. Open the folder in your browser and extract the Folder ID from the URL:
   `https://drive.google.com/drive/folders/<GOOGLE_DRIVE_FOLDER_ID>`
3. Set `GOOGLE_DRIVE_FOLDER_ID` in your `.env` file:
   ```env
   GOOGLE_DRIVE_FOLDER_ID=1a2b3c4d5e6f7g8h9i0j
   ```
4. Enable the **Google Drive API** in Google Cloud Console.
5. In n8n, create a **Google Drive OAuth2** credential using your Client ID and Client Secret.
