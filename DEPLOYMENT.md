# Deployment Guide - Streamlit Cloud

## Step 1: Upload Files to GitHub

### Files to Upload:
✅ `app.py`
✅ `database.py`
✅ `sheets_handler.py`
✅ `facebook_handler.py`
✅ `scheduler.py`
✅ `background_jobs.py`
✅ `notifications.py`
✅ `requirements.txt`
✅ `.gitignore`
✅ `README.md`

### Files to NEVER Upload (Keep Local Only):
❌ `credentials.json` - Google credentials
❌ `config.json` - contains secrets
❌ `*.db` - database files

---

## Step 2: Upload to GitHub

### Option A: Using GitHub Web Interface (Easiest)

1. Go to your repository on GitHub
2. Click **"Add file"** → **"Upload files"**
3. Drag and drop all the files listed above
4. Click **"Commit changes"**

### Option B: Using Git Command Line

```bash
cd ~/Confessions

# Initialize git (if not already)
git init

# Add remote
git remote add origin https://github.com/YOUR_USERNAME/content-approval-system.git

# Add files
git add app.py database.py sheets_handler.py facebook_handler.py scheduler.py background_jobs.py notifications.py requirements.txt .gitignore README.md

# Commit
git commit -m "Initial commit"

# Push
git push -u origin main
```

---

## Step 3: Deploy to Streamlit Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io/)
2. Click **"New app"**
3. Choose your repository: `YOUR_USERNAME/content-approval-system`
4. Main file path: `app.py`
5. Click **"Deploy"**

---

## Step 4: Add Google Credentials to Streamlit Secrets

Since you can't upload `credentials.json` to GitHub, you need to add it as a secret:

1. In Streamlit Cloud, click on your app
2. Click **"Settings"** (gear icon)
3. Go to **"Secrets"** tab
4. Add this (replace with your actual credentials):

```toml
[google_credentials]
type = "service_account"
project_id = "your-project-id"
private_key_id = "your-private-key-id"
private_key = "-----BEGIN PRIVATE KEY-----\nYour-Private-Key-Here\n-----END PRIVATE KEY-----\n"
client_email = "your-service-account@project.iam.gserviceaccount.com"
client_id = "your-client-id"
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account%40project.iam.gserviceaccount.com"
```

**How to get this:**
1. Open your local `credentials.json` file
2. Copy each field
3. **IMPORTANT**: For `private_key`, replace actual newlines with `\n`
   - Example: `"-----BEGIN PRIVATE KEY-----\nYOUR_KEY\n-----END PRIVATE KEY-----\n"`

5. Click **"Save"**

---

## Step 5: Configure the App

1. Wait for app to deploy (1-2 minutes)
2. Open your deployed app
3. Go to **Settings** page
4. Fill in:
   - **Google Sheet ID**: Your sheet ID
   - **Google Credentials File**: Leave as `credentials.json` (it will use secrets)
   - **Facebook Page ID**: Your page ID
   - **Facebook Access Token**: Your token
   - **Gmail Email**: Your Gmail
   - **Gmail App Password**: Your app password
   - **Notification Emails**: Add recipient emails
   - **Posting Windows**: Set your times
   - **Pending Threshold**: Set number (e.g., 20)
   - **App URL**: Your Streamlit app URL (e.g., `https://your-app.streamlit.app`)
5. Click **"Save Configuration"**
6. Click **"Sync Now"** to test

---

## Step 6: Test Everything

1. **Test Google Sheets**: Click "Sync Now" - should see entries
2. **Test Notifications**: Click "Send Test Notification" - check email
3. **Approve an entry**: Should schedule it
4. **Test Facebook** (optional): Publish a scheduled post manually

---

## Important Notes

### Background Jobs Won't Run Automatically
Streamlit Cloud doesn't support background jobs (`background_jobs.py`). You have two options:

**Option A: Manual Sync (Simplest)**
- Just click "Sync Now" whenever you want to check for new entries
- Posts will still publish automatically when their scheduled time arrives (when you open the app)

**Option B: Use GitHub Actions (Advanced)**
Create `.github/workflows/sync.yml`:
```yaml
name: Sync Content
on:
  schedule:
    - cron: '0 0 * * *'  # Daily at midnight
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger sync
        run: curl -X POST https://your-app.streamlit.app/?sync=true
```

**Option C: External Scheduler**
- Use a service like [cron-job.org](https://cron-job.org) to ping your app URL daily
- Or use AWS Lambda / Google Cloud Functions

---

## Troubleshooting

### "ModuleNotFoundError"
- Make sure `requirements.txt` is uploaded
- Restart the app in Streamlit Cloud

### "Google Sheets Authentication Failed"
- Check that secrets are formatted correctly in TOML
- Make sure `private_key` has `\n` instead of actual newlines
- Verify service account email has access to the sheet

### "Facebook API Error"
- Verify your Page Access Token is valid
- Make sure you used Page Access Token (not User Access Token)
- Check token hasn't expired

### "Gmail Authentication Failed"
- Verify you're using App Password, not regular password
- Make sure 2-Factor Authentication is enabled on Gmail

---

## Updating Your App

1. Make changes locally
2. Push to GitHub:
```bash
git add .
git commit -m "Update features"
git push
```
3. Streamlit Cloud will auto-deploy

---

## Your App URL

After deployment, your app will be at:
```
https://your-username-content-approval-system-app-xxxxx.streamlit.app
```

Share this URL with your team!

---

## Cost

**100% FREE:**
- Streamlit Cloud: Free tier (unlimited public apps)
- GitHub: Free for public repositories
- Google Sheets API: Free
- Facebook API: Free
- Gmail SMTP: 500 emails/day free

No credit card needed! 🎉
