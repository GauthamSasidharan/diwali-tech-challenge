# Diwali Tech Challenge - Flask Application

## Folder structure (this is required - Flask looks for these exact folders)

    diwali_tech_campaign/
      app.py
      requirements.txt
      templates/   index, quiz, lamp, phone, departments, offer, admin (.html)
      static/css/style.css
      static/js/script.js
      static/images/avatars/avatar1-4.svg

## Run

    python -m venv venv
    venv\\Scripts\\activate        (Windows)   |   source venv/bin/activate   (Mac/Linux)
    pip install -r requirements.txt
    python app.py

Open http://127.0.0.1:5000

## Admin

http://127.0.0.1:5000/admin is now password protected (HTTP Basic).
Default login for local testing: admin / diwali123.
Set these before going live:

    SECRET_KEY=<long random string>
    ADMIN_USER=<name>
    ADMIN_PASSWORD=<strong password>

## Before going live
- Use HTTPS, and consider PostgreSQL for heavy traffic.
- Review the consent wording and publish offer terms/validity.
- Add your institution logo.
