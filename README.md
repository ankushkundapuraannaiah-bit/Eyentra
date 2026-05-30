# PhotoShare — Secure Photo Sharing with QR Codes

A Python + Flask web application that lets users:

- Register with their **mobile number** (verified via OTP)
- **Upload photos** that are stored in the database (as files on disk)
- Receive an **auto-generated share password** and **QR code** for every photo
- Share the QR code — viewers scan it and enter the password to see the photo
- See **who viewed their photo** (the viewer's mobile number is recorded with a unique View ID)


## Project structure

```
photoshare/
├── app.py                  ← Main Flask application (all routes + models)
├── requirements.txt        ← Python dependencies
├── photoshare.db           ← SQLite database (auto-created on first run)
├── static/
│   ├── css/style.css       ← Stylesheet
│   ├── uploads/            ← Uploaded photos (auto-created)
│   └── qrcodes/            ← Generated QR code images (auto-created)
└── templates/
    ├── base.html           ← Shared navbar / layout
    ├── index.html          ← Home / landing page
    ├── register.html       ← Registration form
    ├── verify_otp.html     ← OTP entry form
    ├── login.html          ← Login form
    ├── dashboard.html      ← User dashboard
    ├── upload.html         ← Photo upload form
    ├── photo_detail.html   ← Single photo view with QR and view log
    ├── view_photo.html     ← Public QR landing page (password gate)
    └── error.html          ← Generic error page
```


## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the server

```bash
python app.py
```

### 3. Open in your browser

```
http://127.0.0.1:5000
```


## Deploy on Vercel

This repository now includes Vercel-ready files at the project root:

- `vercel.json`
- `requirements.txt`
- `api/index.py`

The production app uses a web database through `DATABASE_URL` or `POSTGRES_URL`.
Use Vercel Postgres, Neon, Supabase Postgres, or any hosted PostgreSQL database.

### Required Vercel environment variables

Set these in Vercel Project Settings -> Environment Variables:

```text
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE
SECRET_KEY=use-a-long-random-secret
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_FROM_NUMBER=+1234567890
```

If Vercel gives you `POSTGRES_URL` instead of `DATABASE_URL`, the app will use it automatically.

### Important production changes

- SQLite is only used for local development.
- Uploaded images are stored in the database as binary data.
- QR codes are stored in the database as binary data.
- Viewer analytics charts are generated on demand, not saved permanently to disk.
- Local folders under `photoshare/static/uploads`, `qrcodes`, and `analytics` are ignored by Git.

### Deploy flow

1. Push this project to `ankushkundapuraannaiah-bit/Eyentra`.
2. Import the GitHub repo in Vercel.
3. Keep the Vercel project root as the repository root.
4. Add the environment variables above.
5. Deploy.


## OTP (SMS) during development

In development, the OTP is **printed to the terminal** instead of being sent via SMS.
Look for a block like this in your terminal output:

```
==================================================
  [SMS SIMULATION]  To: 9876543210   OTP: 482931
==================================================
```

To send real SMS, replace `send_otp_simulation()` in `app.py` with a call to
your preferred SMS gateway (e.g. Twilio, MSG91, Fast2SMS).


## How the photo sharing flow works

1. User uploads a photo → the app saves the image file and creates a `Photo` record.
2. A random **share password** (10 characters) and a **share token** (UUID) are stored.
3. A **QR code** is generated that encodes the URL `/view/<share_token>`.
4. The owner sees the photo, the QR code, and the share password on the detail page.
5. The owner shares the QR image (download button available) with someone.
6. The recipient scans the QR → lands on the password gate page.
7. The recipient enters the share password (and their mobile number if not logged in).
8. A `ViewLog` record is saved with a unique **View ID** and the viewer's mobile number.
9. The photo owner sees the view log on the dashboard and photo detail page.


## Security notes

- Passwords are stored as **bcrypt hashes** (via `werkzeug.security`).
- Share passwords are single-use secrets — anyone with both the QR code and the
  password can view the photo.  Treat the share password like a PIN.
- For production, replace `app.secret_key` with a long random string stored in
  an environment variable.
- Add HTTPS (e.g. via nginx + Let's Encrypt) before deploying publicly.
- For real OTP delivery, integrate an SMS gateway and add rate-limiting to
  prevent OTP brute-force attacks.


## Extending the project

| Feature                     | Where to add it                              |
|-----------------------------|----------------------------------------------|
| Real SMS OTP                | Replace `send_otp_simulation()` in `app.py`  |
| Email notifications         | Add Flask-Mail and call it from `view_shared_photo()` |
| Photo expiry / view limit   | Add `expires_at` / `max_views` column to `Photo` |
| Multiple photos per post    | Change upload route to accept multiple files |
| Admin panel                 | Add Flask-Admin                              |
| Production database         | Change `SQLALCHEMY_DATABASE_URI` to PostgreSQL |
