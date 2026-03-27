#!/usr/bin/env python3
import os
import logging
import sqlite3
import threading
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from decimal import Decimal
from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect, current_app

# --- Configuration via environment variables ---
DB_NAME = os.getenv('DB_NAME', os.path.expanduser('~/farmyard.db'))
STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')

# --- Notification configuration (via environment variables) ---
SMTP_HOST = os.getenv('SMTP_HOST')  # e.g. smtp.gmail.com or localhost for debug server
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER')  # SMTP username (email)
SMTP_PASS = os.getenv('SMTP_PASS')  # SMTP password or app password
FROM_EMAIL = os.getenv('FROM_EMAIL') or SMTP_USER or 'no-reply@example.com'
FARMER_EMAIL = os.getenv('FARMER_EMAIL')  # farmer's email address

TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_FROM = os.getenv('TWILIO_FROM_NUMBER')  # e.g. +1234567890 (Twilio number)
FARMER_PHONE = os.getenv('FARMER_PHONE')  # farmer phone number in E.164 e.g. +2547xxxxxxx

# Optional: Twilio for SMS
try:
    from twilio.rest import Client as TwilioClient
    _TWILIO_AVAILABLE = True
except Exception:
    _TWILIO_AVAILABLE = False

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
app.secret_key = os.getenv('FLASK_SECRET', 'dev-secret')  # set a real secret in production

# --- Fallback sample products (used if DB is unavailable) ---
PRODUCTS = [
    {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggdozen.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
    {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
    {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
    {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
    {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
    {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
]

# configure logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- SQLite helpers ---
def get_db():
    if 'db' in g:
        return g.db
    db_path = DB_NAME
    if not os.path.isabs(db_path):
        db_path = os.path.expanduser(db_path)
    parent = os.path.dirname(db_path) or '.'
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    g.db = conn
    return conn

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass

def rows_to_json_safe(rows):
    out = []
    for r in rows:
        row = dict(r)
        price_val = row.get('price')
        if isinstance(price_val, Decimal):
            row['price'] = float(price_val)
        elif isinstance(price_val, (int, float)):
            row['price'] = float(price_val)
        out.append(row)
    return out

def ensure_tables():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          excerpt TEXT,
          description TEXT,
          price INTEGER NOT NULL,
          unit TEXT,
          image TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER,
          buyer_name TEXT NOT NULL,
          buyer_phone TEXT NOT NULL,
          buyer_email TEXT,
          quantity INTEGER NOT NULL DEFAULT 1,
          address TEXT,
          notes TEXT,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    db.commit()
    cur.close()

# --- Notifications (email with inline image + SMS) ---
def send_email_with_image(to_address, subject, body_text, image_path=None, image_cid=None):
    """Send plain-text + HTML email with optional inline image (image_path is filesystem path)."""
    try:
        if not SMTP_HOST:
            logger.warning("SMTP_HOST not configured; skipping email to %s", to_address)
            return False

        from_addr = FROM_EMAIL or SMTP_USER or 'no-reply@example.com'
        msg = EmailMessage()
        msg['From'] = from_addr
        msg['To'] = to_address
        msg['Subject'] = subject

        # Build HTML if image present
        if image_path and Path(image_path).is_file():
            cid = image_cid or make_msgid(domain='farmyard.local')
            html = f"""<html>
                <body>
                  <pre style="font-family:system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial;">{body_text}</pre>
                  <p><img src="cid:{cid[1:-1]}" alt="product image" style="max-width:320px; height:auto; border-radius:6px;"/></p>
                </body>
                </html>"""
            msg.set_content(body_text)
            msg.add_alternative(html, subtype='html')

            with open(image_path, 'rb') as imgf:
                img_data = imgf.read()
            ext = Path(image_path).suffix.lstrip('.').lower()
            subtype = ext if ext in ('jpeg','jpg','png','gif','webp') else 'jpeg'
            try:
                # attach related to the HTML part
                msg.get_payload()[1].add_related(img_data, maintype='image', subtype=subtype, cid=cid)
            except Exception:
                msg.add_attachment(img_data, maintype='image', subtype=subtype, filename=os.path.basename(image_path))
        else:
            msg.set_content(body_text)

        # Connect and send
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.ehlo()
                if SMTP_USER and SMTP_PASS:
                    smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.set_debuglevel(0)
                smtp.ehlo()
                if SMTP_PORT in (587, 25):
                    try:
                        smtp.starttls()
                        smtp.ehlo()
                    except Exception:
                        logger.debug("STARTTLS failed or not supported")
                if SMTP_USER and SMTP_PASS:
                    smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)

        logger.info("Email sent to %s", to_address)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_address)
        return False

def send_sms(to_number, message):
    """Send SMS via Twilio if configured. Returns True on success."""
    if not _TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
        logger.warning("Twilio not configured; skipping SMS to %s", to_number)
        return False
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=to_number)
        logger.info("SMS sent to %s", to_number)
        return True
    except Exception:
        logger.exception("Failed to send SMS to %s", to_number)
        return False

def notify_async(fn, *args, **kwargs):
    """Run notification function in a daemon thread to avoid blocking requests."""
    try:
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()
    except Exception:
        logger.exception("Failed to start notification thread")

def send_order_notifications(order_id, product, payload):
    """
    Compose and send notifications for a new order.
    - order_id: int
    - product: dict (id,title,unit,price,image,...)
    - payload: dict (buyer_name,buyer_phone,buyer_email,quantity,address,notes)
    """
    farmer_subject = f"New order #{order_id} — {product.get('title')}"
    farmer_body = (
        f"New order received\n\n"
        f"Order ID: {order_id}\n"
        f"Product: {product.get('title')} (ID: {product.get('id')})\n"
        f"Quantity: {payload.get('quantity')}\n"
        f"Unit: {product.get('unit','')}\n\n"
        f"Buyer name: {payload.get('buyer_name')}\n"
        f"Buyer phone: {payload.get('buyer_phone')}\n"
        f"Buyer email: {payload.get('buyer_email')}\n"
        f"Delivery address: {payload.get('address')}\n"
        f"Notes: {payload.get('notes')}\n\n"
        f"Please contact the buyer to confirm and arrange delivery.\n"
    )

    buyer_subject = f"Order received — #{order_id}"
    buyer_body = (
        f"Thank you {payload.get('buyer_name')},\n\n"
        f"We have received your order (ID: {order_id}) for {product.get('title')} x{payload.get('quantity')}.\n"
        f"Our team will contact you shortly to confirm details and delivery.\n\n"
        f"Order summary:\n"
        f"- Product: {product.get('title')}\n"
        f"- Quantity: {payload.get('quantity')} {product.get('unit','')}\n"
        f"- Delivery address: {payload.get('address')}\n\n"
        f"Please wait for confirmation. Thank you for ordering from Farmyard."
    )

    farmer_sms = f"New order #{order_id}: {product.get('title')} x{payload.get('quantity')}. Buyer: {payload.get('buyer_name')} {payload.get('buyer_phone')}."
    buyer_sms = f"Order #{order_id} received. Thank you {payload.get('buyer_name')}. We'll contact you shortly."

    # Determine image path (static images folder)
    image_path = None
    img_name = product.get('image') or ''
    if img_name:
        static_dir = os.path.join(os.path.dirname(__file__), 'static', STATIC_IMAGE_FOLDER)
        candidate = os.path.join(static_dir, img_name)
        if os.path.isfile(candidate):
            image_path = candidate

    # Send farmer email with inline image if available
    if FARMER_EMAIL:
        notify_async(send_email_with_image, FARMER_EMAIL, farmer_subject, farmer_body, image_path)
    else:
        logger.warning("FARMER_EMAIL not set; skipping farmer email")

    # Farmer SMS
    if FARMER_PHONE:
        notify_async(send_sms, FARMER_PHONE, farmer_sms)
    else:
        logger.warning("FARMER_PHONE not set; skipping farmer SMS")

    # Buyer notifications
    if payload.get('buyer_email'):
        notify_async(send_email_with_image, payload.get('buyer_email'), buyer_subject, buyer_body, image_path)
    else:
        logger.info("Buyer email not provided; skipping buyer email")

    if payload.get('buyer_phone'):
        notify_async(send_sms, payload.get('buyer_phone'), buyer_sms)
    else:
        logger.info("Buyer phone not provided; skipping buyer SMS")

# --- Data access functions (SQLite, using ? placeholders) ---
def fetch_all_products(category=None, q=None):
    try:
        db = get_db()
        sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
        params = []
        where = []
        if category and category.lower() not in ('all', ''):
            where.append("category = ?")
            params.append(category)
        if q:
            where.append("(title LIKE ? OR excerpt LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        cur = db.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows_to_json_safe(rows)
    except Exception:
        logger.exception("DB error in fetch_all_products")
    # fallback to in-memory PRODUCTS
    results = PRODUCTS.copy()
    if category and category.lower() not in ('all', ''):
        results = [p for p in results if p.get('category') == category]
    if q:
        qlow = q.lower()
        results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
    return results

def fetch_product_by_id(product_id):
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id = ?", (product_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row = dict(row)
            if isinstance(row.get('price'), Decimal):
                row['price'] = float(row['price'])
            elif isinstance(row.get('price'), (int, float)):
                row['price'] = float(row['price'])
            return row
        return None
    except Exception:
        logger.exception("DB error in fetch_product_by_id")
    return next((x for x in PRODUCTS if x['id'] == product_id), None)

def insert_order(product_id, name, phone, email, quantity, address, notes):
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (?,?,?,?,?,?,?)",
            (product_id, name, phone, email, quantity, address, notes)
        )
        order_id = cur.lastrowid
        db.commit()
        cur.close()
        return order_id
    except Exception:
        logger.exception("DB error in insert_order")
        try:
            db.rollback()
        except Exception:
            pass
    return None

@app.context_processor
def utility_processor():
    def endpoint_exists(name):
        return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
    return dict(endpoint_exists=endpoint_exists)

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/shop')
def shop():
    cat = request.args.get('category')
    q = request.args.get('q')
    products = fetch_all_products(category=cat, q=q)
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
        cats = [r['category'] for r in cur.fetchall()]
        cur.close()
    except Exception:
        logger.warning("DB unavailable when fetching categories; using fallback categories")
        cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
    categories = ['all'] + cats
    active_category = cat or 'all'
    return render_template('shop.html',
                           products=products,
                           categories=categories,
                           active_category=active_category,
                           static_image_folder=STATIC_IMAGE_FOLDER)

@app.route('/api/product/<int:product_id>')
def product_api(product_id):
    p = fetch_product_by_id(product_id)
    if not p:
        return jsonify({'error': 'not found'}), 404
    return jsonify(p)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    p = fetch_product_by_id(product_id)
    if not p:
        abort(404)
    return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

@app.route('/order/<int:product_id>', methods=['GET', 'POST'])
def order(product_id):
    if request.method == 'GET':
        return render_template('order_form.html', product_id=product_id)

    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form.to_dict()

    name = (data.get('buyer_name') or '').strip()
    phone = (data.get('buyer_phone') or '').strip()
    email = (data.get('buyer_email') or '').strip()
    try:
        quantity = int(data.get('quantity') or 1)
    except Exception:
        quantity = 1
    address = data.get('address') or ''
    notes = data.get('notes') or ''

    if not name or not phone or quantity < 1:
        return jsonify({'error': 'Missing required fields'}), 400

    p = fetch_product_by_id(product_id)
    if not p:
        return jsonify({'error': 'Product not found'}), 404

    try:
        order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
        if not order_id:
            app.logger.error("Order insert returned no id (DB may be down)")
            return jsonify({'error': 'Server error'}), 500

        # Prepare payload and trigger notifications in background
        payload = {
            'buyer_name': name,
            'buyer_phone': phone,
            'buyer_email': email,
            'quantity': quantity,
            'address': address,
            'notes': notes
        }
        notify_async(send_order_notifications, order_id, p, payload)

        return jsonify({'success': True, 'order_id': order_id}), 201
    except Exception:
        app.logger.exception("Order insert failed")
        return jsonify({'error': 'Server error'}), 500

@app.route('/projects')
def projects():
    return render_template('projects.html', title="Projects — Farmyard")

@app.route('/about')
def about():
    return render_template('about.html', title="About — Farmyard")

@app.route('/contact', methods=['GET','POST'])
def contact():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        message = (request.form.get('message') or '').strip()
        subject = (request.form.get('subject') or 'Website contact').strip()

        if not name or not email or not message:
            flash('Please fill in the required fields.', 'danger')
            return redirect(url_for('contact'))

        farmer_subject = f"Contact form: {subject} — {name}"
        farmer_body = (
            f"New contact form submission\n\n"
            f"Name: {name}\n"
            f"Email: {email}\n"
            f"Phone: {phone}\n"
            f"Subject: {subject}\n\n"
            f"Message:\n{message}\n\n"
            f"Please respond to the enquirer."
        )

        visitor_subject = "Thanks — we received your message"
        visitor_body = (
            f"Hi {name},\n\n"
            f"Thanks for contacting Farmyard. We received your message and will reply within 24–48 hours.\n\n"
            f"Your message:\n{message}\n\n"
            f"Best regards,\nFarmyard Team"
        )

        # Send notifications asynchronously
        if FARMER_EMAIL:
            notify_async(send_email_with_image, FARMER_EMAIL, farmer_subject, farmer_body, None)
        else:
            app.logger.warning("FARMER_EMAIL not set; skipping farmer email")

        if FARMER_PHONE:
            notify_async(send_sms, FARMER_PHONE, f"Contact from {name}: {subject} — {phone or 'no phone provided'}")
        else:
            app.logger.warning("FARMER_PHONE not set; skipping farmer SMS")

        if email:
            notify_async(send_email_with_image, email, visitor_subject, visitor_body, None)
        else:
            app.logger.info("Visitor did not provide email; skipping confirmation email")

        flash('Message sent — thank you!', 'success')
        return redirect(url_for('contact'))

    return render_template('contact.html', title="Contact — Farmyard")

@app.route('/privacy')
def privacy():
    return render_template('privacy.html', title='Privacy Policy')

@app.route('/terms')
def terms():
    return render_template('terms.html', title='Terms of Service')

if __name__ == '__main__':
    try:
        ensure_tables()
    except Exception:
        logger.exception('ensure_tables failed at startup')
    # Run on 0.0.0.0 so you can test from other devices on the network if needed
    app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))




# #!/usr/bin/env python3
# import os
# import logging
# import sqlite3
# import threading
# import smtplib
# from email.message import EmailMessage
# from email.utils import make_msgid
# from pathlib import Path
# from decimal import Decimal
# from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect, current_app

# # --- Configuration via environment variables ---
# DB_NAME = os.getenv('DB_NAME', os.path.expanduser('~/farmyard.db'))
# STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')

# # --- Notification configuration (via environment variables) ---
# SMTP_HOST = os.getenv('SMTP_HOST')  # e.g. smtp.gmail.com or localhost for debug server
# SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
# SMTP_USER = os.getenv('SMTP_USER')  # SMTP username (email)
# SMTP_PASS = os.getenv('SMTP_PASS')  # SMTP password or app password
# FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USER)
# FARMER_EMAIL = os.getenv('FARMER_EMAIL')  # farmer's email address

# TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
# TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
# TWILIO_FROM = os.getenv('TWILIO_FROM_NUMBER')  # e.g. +1234567890 (Twilio number)
# FARMER_PHONE = os.getenv('FARMER_PHONE')  # farmer phone number in E.164 e.g. +2547xxxxxxx

# # Optional: Twilio for SMS
# try:
#     from twilio.rest import Client as TwilioClient
#     _TWILIO_AVAILABLE = True
# except Exception:
#     _TWILIO_AVAILABLE = False

# app = Flask(__name__)
# app.config['JSON_SORT_KEYS'] = False

# # --- Fallback sample products (used if DB is unavailable) ---
# PRODUCTS = [
#     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggdozen.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
#     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
#     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
#     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
#     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
#     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# ]

# # configure logger
# logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO)

# # --- SQLite helpers ---
# def get_db():
#     if 'db' in g:
#         return g.db
#     db_path = DB_NAME
#     if not os.path.isabs(db_path):
#         db_path = os.path.expanduser(db_path)
#     parent = os.path.dirname(db_path) or '.'
#     os.makedirs(parent, exist_ok=True)
#     conn = sqlite3.connect(db_path, check_same_thread=False)
#     conn.row_factory = sqlite3.Row
#     g.db = conn
#     return conn

# @app.teardown_appcontext
# def close_db(exc):
#     db = g.pop('db', None)
#     if db is not None:
#         try:
#             db.close()
#         except Exception:
#             pass

# def rows_to_json_safe(rows):
#     out = []
#     for r in rows:
#         row = dict(r)
#         price_val = row.get('price')
#         if isinstance(price_val, Decimal):
#             row['price'] = float(price_val)
#         elif isinstance(price_val, (int, float)):
#             row['price'] = float(price_val)
#         out.append(row)
#     return out

# def ensure_tables():
#     db = get_db()
#     cur = db.cursor()
#     cur.execute("""
#         CREATE TABLE IF NOT EXISTS products (
#           id INTEGER PRIMARY KEY AUTOINCREMENT,
#           category TEXT NOT NULL,
#           title TEXT NOT NULL,
#           excerpt TEXT,
#           description TEXT,
#           price INTEGER NOT NULL,
#           unit TEXT,
#           image TEXT
#         );
#     """)
#     cur.execute("""
#         CREATE TABLE IF NOT EXISTS orders (
#           id INTEGER PRIMARY KEY AUTOINCREMENT,
#           product_id INTEGER,
#           buyer_name TEXT NOT NULL,
#           buyer_phone TEXT NOT NULL,
#           buyer_email TEXT,
#           quantity INTEGER NOT NULL DEFAULT 1,
#           address TEXT,
#           notes TEXT,
#           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#         );
#     """)
#     db.commit()
#     cur.close()

# # --- Notifications (email with inline image + SMS) ---
# def send_email_with_image(to_address, subject, body_text, image_path=None, image_cid=None):
#     """Send plain-text + HTML email with optional inline image (image_path is filesystem path)."""
#     try:
#         if not SMTP_HOST:
#             logger.warning("SMTP_HOST not configured; skipping email to %s", to_address)
#             return False

#         msg = EmailMessage()
#         msg['From'] = FROM_EMAIL or 'no-reply@example.com'
#         msg['To'] = to_address
#         msg['Subject'] = subject

#         # If image provided, send a simple HTML body with inline image; otherwise plain text
#         if image_path and Path(image_path).is_file():
#             cid = image_cid or make_msgid(domain='farmyard.local')
#             html = f"""<html>
#                 <body>
#                   <pre style="font-family:system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial;">{body_text}</pre>
#                   <p><img src="cid:{cid[1:-1]}" alt="product image" style="max-width:320px; height:auto; border-radius:6px;"/></p>
#                 </body>
#                 </html>"""
#             msg.set_content(body_text)
#             msg.add_alternative(html, subtype='html')
#             with open(image_path, 'rb') as imgf:
#                 img_data = imgf.read()
#             maintype, subtype = 'image', Path(image_path).suffix.lstrip('.').lower() or 'jpeg'
#             # attach related image to the HTML part
#             try:
#                 msg.get_payload()[1].add_related(img_data, maintype=maintype, subtype=subtype, cid=cid)
#             except Exception:
#                 # fallback: attach as normal attachment if related fails
#                 msg.add_attachment(img_data, maintype=maintype, subtype=subtype, filename=os.path.basename(image_path))
#         else:
#             msg.set_content(body_text)

#         with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
#             smtp.ehlo()
#             if SMTP_PORT in (587, 25):
#                 try:
#                     smtp.starttls()
#                     smtp.ehlo()
#                 except Exception:
#                     pass
#             if SMTP_USER and SMTP_PASS:
#                 smtp.login(SMTP_USER, SMTP_PASS)
#             smtp.send_message(msg)
#         logger.info("Email sent to %s", to_address)
#         return True
#     except Exception:
#         logger.exception("Failed to send email to %s", to_address)
#         return False

# def send_sms(to_number, message):
#     """Send SMS via Twilio if configured. Returns True on success."""
#     if not _TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
#         logger.warning("Twilio not configured; skipping SMS to %s", to_number)
#         return False
#     try:
#         client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
#         client.messages.create(body=message, from_=TWILIO_FROM, to=to_number)
#         logger.info("SMS sent to %s", to_number)
#         return True
#     except Exception:
#         logger.exception("Failed to send SMS to %s", to_number)
#         return False

# def notify_async(fn, *args, **kwargs):
#     """Run notification function in a daemon thread to avoid blocking requests."""
#     try:
#         t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
#         t.start()
#     except Exception:
#         logger.exception("Failed to start notification thread")

# def send_order_notifications(order_id, product, payload):
#     """
#     Compose and send notifications for a new order.
#     - order_id: int
#     - product: dict (id,title,unit,price,image,...)
#     - payload: dict (buyer_name,buyer_phone,buyer_email,quantity,address,notes)
#     """
#     farmer_subject = f"New order #{order_id} — {product.get('title')}"
#     farmer_body = (
#         f"New order received\n\n"
#         f"Order ID: {order_id}\n"
#         f"Product: {product.get('title')} (ID: {product.get('id')})\n"
#         f"Quantity: {payload.get('quantity')}\n"
#         f"Unit: {product.get('unit','')}\n\n"
#         f"Buyer name: {payload.get('buyer_name')}\n"
#         f"Buyer phone: {payload.get('buyer_phone')}\n"
#         f"Buyer email: {payload.get('buyer_email')}\n"
#         f"Delivery address: {payload.get('address')}\n"
#         f"Notes: {payload.get('notes')}\n\n"
#         f"Please contact the buyer to confirm and arrange delivery.\n"
#     )

#     buyer_subject = f"Order received — #{order_id}"
#     buyer_body = (
#         f"Thank you {payload.get('buyer_name')},\n\n"
#         f"We have received your order (ID: {order_id}) for {product.get('title')} x{payload.get('quantity')}.\n"
#         f"Our team will contact you shortly to confirm details and delivery.\n\n"
#         f"Order summary:\n"
#         f"- Product: {product.get('title')}\n"
#         f"- Quantity: {payload.get('quantity')} {product.get('unit','')}\n"
#         f"- Delivery address: {payload.get('address')}\n\n"
#         f"Please wait for confirmation. Thank you for ordering from Farmyard."
#     )

#     farmer_sms = f"New order #{order_id}: {product.get('title')} x{payload.get('quantity')}. Buyer: {payload.get('buyer_name')} {payload.get('buyer_phone')}."
#     buyer_sms = f"Order #{order_id} received. Thank you {payload.get('buyer_name')}. We'll contact you shortly."

#     # Determine image path (static images folder)
#     image_path = None
#     img_name = product.get('image') or ''
#     if img_name:
#         static_dir = os.path.join(os.path.dirname(__file__), 'static', STATIC_IMAGE_FOLDER)
#         candidate = os.path.join(static_dir, img_name)
#         if os.path.isfile(candidate):
#             image_path = candidate

#     # Send farmer email with inline image if available
#     if FARMER_EMAIL:
#         notify_async(send_email_with_image, FARMER_EMAIL, farmer_subject, farmer_body, image_path)
#     else:
#         logger.warning("FARMER_EMAIL not set; skipping farmer email")

#     # Farmer SMS
#     if FARMER_PHONE:
#         notify_async(send_sms, FARMER_PHONE, farmer_sms)
#     else:
#         logger.warning("FARMER_PHONE not set; skipping farmer SMS")

#     # Buyer notifications
#     if payload.get('buyer_email'):
#         notify_async(send_email_with_image, payload.get('buyer_email'), buyer_subject, buyer_body, image_path)
#     else:
#         logger.info("Buyer email not provided; skipping buyer email")

#     if payload.get('buyer_phone'):
#         notify_async(send_sms, payload.get('buyer_phone'), buyer_sms)
#     else:
#         logger.info("Buyer phone not provided; skipping buyer SMS")

# # --- Data access functions (SQLite, using ? placeholders) ---
# def fetch_all_products(category=None, q=None):
#     try:
#         db = get_db()
#         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
#         params = []
#         where = []
#         if category and category.lower() not in ('all', ''):
#             where.append("category = ?")
#             params.append(category)
#         if q:
#             where.append("(title LIKE ? OR excerpt LIKE ?)")
#             like = f"%{q}%"
#             params.extend([like, like])
#         if where:
#             sql += " WHERE " + " AND ".join(where)
#         sql += " ORDER BY id DESC"
#         cur = db.cursor()
#         cur.execute(sql, params)
#         rows = cur.fetchall()
#         cur.close()
#         return rows_to_json_safe(rows)
#     except Exception:
#         logger.exception("DB error in fetch_all_products")
#     # fallback to in-memory PRODUCTS
#     results = PRODUCTS.copy()
#     if category and category.lower() not in ('all', ''):
#         results = [p for p in results if p.get('category') == category]
#     if q:
#         qlow = q.lower()
#         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
#     return results

# def fetch_product_by_id(product_id):
#     try:
#         db = get_db()
#         cur = db.cursor()
#         cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id = ?", (product_id,))
#         row = cur.fetchone()
#         cur.close()
#         if row:
#             row = dict(row)
#             if isinstance(row.get('price'), Decimal):
#                 row['price'] = float(row['price'])
#             elif isinstance(row.get('price'), (int, float)):
#                 row['price'] = float(row['price'])
#             return row
#         return None
#     except Exception:
#         logger.exception("DB error in fetch_product_by_id")
#     return next((x for x in PRODUCTS if x['id'] == product_id), None)

# def insert_order(product_id, name, phone, email, quantity, address, notes):
#     try:
#         db = get_db()
#         cur = db.cursor()
#         cur.execute(
#             "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (?,?,?,?,?,?,?)",
#             (product_id, name, phone, email, quantity, address, notes)
#         )
#         order_id = cur.lastrowid
#         db.commit()
#         cur.close()
#         return order_id
#     except Exception:
#         logger.exception("DB error in insert_order")
#         try:
#             db.rollback()
#         except Exception:
#             pass
#     return None

# @app.context_processor
# def utility_processor():
#     def endpoint_exists(name):
#         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
#     return dict(endpoint_exists=endpoint_exists)

# # --- Routes ---
# @app.route('/')
# def index():
#     return render_template('index.html')

# @app.route('/shop')
# def shop():
#     cat = request.args.get('category')
#     q = request.args.get('q')
#     products = fetch_all_products(category=cat, q=q)
#     try:
#         db = get_db()
#         cur = db.cursor()
#         cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
#         cats = [r['category'] for r in cur.fetchall()]
#         cur.close()
#     except Exception:
#         logger.warning("DB unavailable when fetching categories; using fallback categories")
#         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
#     categories = ['all'] + cats
#     active_category = cat or 'all'
#     return render_template('shop.html',
#                            products=products,
#                            categories=categories,
#                            active_category=active_category,
#                            static_image_folder=STATIC_IMAGE_FOLDER)

# @app.route('/api/product/<int:product_id>')
# def product_api(product_id):
#     p = fetch_product_by_id(product_id)
#     if not p:
#         return jsonify({'error': 'not found'}), 404
#     return jsonify(p)

# @app.route('/product/<int:product_id>')
# def product_detail(product_id):
#     p = fetch_product_by_id(product_id)
#     if not p:
#         abort(404)
#     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

# @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# def order(product_id):
#     if request.method == 'GET':
#         return render_template('order_form.html', product_id=product_id)

#     if request.is_json:
#         data = request.get_json() or {}
#     else:
#         data = request.form.to_dict()

#     name = (data.get('buyer_name') or '').strip()
#     phone = (data.get('buyer_phone') or '').strip()
#     email = (data.get('buyer_email') or '').strip()
#     try:
#         quantity = int(data.get('quantity') or 1)
#     except Exception:
#         quantity = 1
#     address = data.get('address') or ''
#     notes = data.get('notes') or ''

#     if not name or not phone or quantity < 1:
#         return jsonify({'error': 'Missing required fields'}), 400

#     p = fetch_product_by_id(product_id)
#     if not p:
#         return jsonify({'error': 'Product not found'}), 404

#     try:
#         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
#         if not order_id:
#             app.logger.error("Order insert returned no id (DB may be down)")
#             return jsonify({'error': 'Server error'}), 500

#         # Prepare payload and trigger notifications in background
#         payload = {
#             'buyer_name': name,
#             'buyer_phone': phone,
#             'buyer_email': email,
#             'quantity': quantity,
#             'address': address,
#             'notes': notes
#         }
#         notify_async(send_order_notifications, order_id, p, payload)

#         return jsonify({'success': True, 'order_id': order_id}), 201
#     except Exception:
#         app.logger.exception("Order insert failed")
#         return jsonify({'error': 'Server error'}), 500

# @app.route('/projects')
# def projects():
#     return render_template('projects.html', title="Projects — Farmyard")

# @app.route('/about')
# def about():
#     return render_template('about.html', title="About — Farmyard")

# @app.route('/contact', methods=['GET','POST'])
# def contact():
#     if request.method == 'POST':
#         name = request.form.get('name')
#         email = request.form.get('email')
#         message = request.form.get('message')
#         if not name or not email or not message:
#             flash('Please fill in the required fields.', 'danger')
#             return redirect(url_for('contact'))
#         flash('Message sent — thank you!', 'success')
#         return redirect(url_for('contact'))
#     return render_template('contact.html', title="Contact — Farmyard")

# @app.route('/privacy')
# def privacy():
#     return render_template('privacy.html', title='Privacy Policy')

# @app.route('/terms')
# def terms():
#     return render_template('terms.html', title='Terms of Service')

# if __name__ == '__main__':
#     try:
#         ensure_tables()
#     except Exception:
#         logger.exception('ensure_tables failed at startup')
#     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))



# # import os
# # import logging
# # import sqlite3
# # import threading
# # import smtplib
# # from email.message import EmailMessage
# # from email.utils import make_msgid
# # from pathlib import Path
# # from decimal import Decimal
# # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect, current_app

# # # --- Configuration via environment variables ---
# # DB_NAME = os.getenv('DB_NAME', os.path.expanduser('~/farmyard.db'))
# # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')

# # # --- Notification configuration (via environment variables) ---
# # SMTP_HOST = os.getenv('SMTP_HOST')  # e.g. smtp.gmail.com or localhost for debug server
# # SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
# # SMTP_USER = os.getenv('SMTP_USER')  # SMTP username (email)
# # SMTP_PASS = os.getenv('SMTP_PASS')  # SMTP password or app password
# # FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USER)
# # FARMER_EMAIL = os.getenv('FARMER_EMAIL')  # farmer's email address

# # TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
# # TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
# # TWILIO_FROM = os.getenv('TWILIO_FROM_NUMBER')  # e.g. +1234567890 (Twilio number)
# # FARMER_PHONE = os.getenv('FARMER_PHONE')  # farmer phone number in E.164 e.g. +2547xxxxxxx

# # # Optional: Twilio for SMS
# # try:
# #     from twilio.rest import Client as TwilioClient
# #     _TWILIO_AVAILABLE = True
# # except Exception:
# #     _TWILIO_AVAILABLE = False

# # app = Flask(__name__)
# # app.config['JSON_SORT_KEYS'] = False

# # # --- Fallback sample products (used if DB is unavailable) ---
# # PRODUCTS = [
# #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # ]

# # # configure logger
# # logger = logging.getLogger(__name__)
# # logging.basicConfig(level=logging.INFO)

# # # --- SQLite helpers ---
# # def get_db():
# #     if 'db' in g:
# #         return g.db
# #     db_path = DB_NAME
# #     if not os.path.isabs(db_path):
# #         db_path = os.path.expanduser(db_path)
# #     parent = os.path.dirname(db_path) or '.'
# #     os.makedirs(parent, exist_ok=True)
# #     conn = sqlite3.connect(db_path, check_same_thread=False)
# #     conn.row_factory = sqlite3.Row
# #     g.db = conn
# #     return conn

# # @app.teardown_appcontext
# # def close_db(exc):
# #     db = g.pop('db', None)
# #     if db is not None:
# #         try:
# #             db.close()
# #         except Exception:
# #             pass

# # def rows_to_json_safe(rows):
# #     out = []
# #     for r in rows:
# #         row = dict(r)
# #         price_val = row.get('price')
# #         if isinstance(price_val, Decimal):
# #             row['price'] = float(price_val)
# #         elif isinstance(price_val, (int, float)):
# #             row['price'] = float(price_val)
# #         out.append(row)
# #     return out

# # def ensure_tables():
# #     db = get_db()
# #     cur = db.cursor()
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS products (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           category TEXT NOT NULL,
# #           title TEXT NOT NULL,
# #           excerpt TEXT,
# #           description TEXT,
# #           price INTEGER NOT NULL,
# #           unit TEXT,
# #           image TEXT
# #         );
# #     """)
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS orders (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           product_id INTEGER,
# #           buyer_name TEXT NOT NULL,
# #           buyer_phone TEXT NOT NULL,
# #           buyer_email TEXT,
# #           quantity INTEGER NOT NULL DEFAULT 1,
# #           address TEXT,
# #           notes TEXT,
# #           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# #         );
# #     """)
# #     db.commit()
# #     cur.close()

# # # --- Notifications (email with inline image + SMS) ---
# # def send_email_with_image(to_address, subject, body_text, image_path=None, image_cid=None):
# #     """Send plain-text + HTML email with optional inline image (image_path is filesystem path)."""
# #     try:
# #         if not SMTP_HOST:
# #             logger.warning("SMTP_HOST not configured; skipping email to %s", to_address)
# #             return False

# #         msg = EmailMessage()
# #         msg['From'] = FROM_EMAIL or 'no-reply@example.com'
# #         msg['To'] = to_address
# #         msg['Subject'] = subject

# #         # If image provided, send a simple HTML body with inline image; otherwise plain text
# #         if image_path and Path(image_path).is_file():
# #             cid = image_cid or make_msgid(domain='farmyard.local')
# #             html = f"""<html>
# #                 <body>
# #                   <pre style="font-family:system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial;">{body_text}</pre>
# #                   <p><img src="cid:{cid[1:-1]}" alt="product image" style="max-width:320px; height:auto; border-radius:6px;"/></p>
# #                 </body>
# #                 </html>"""
# #             msg.set_content(body_text)
# #             msg.add_alternative(html, subtype='html')
# #             with open(image_path, 'rb') as imgf:
# #                 img_data = imgf.read()
# #             maintype, subtype = 'image', Path(image_path).suffix.lstrip('.').lower() or 'jpeg'
# #             # attach related image to the HTML part
# #             try:
# #                 msg.get_payload()[1].add_related(img_data, maintype=maintype, subtype=subtype, cid=cid)
# #             except Exception:
# #                 # fallback: attach as normal attachment if related fails
# #                 msg.add_attachment(img_data, maintype=maintype, subtype=subtype, filename=os.path.basename(image_path))
# #         else:
# #             msg.set_content(body_text)

# #         with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
# #             smtp.ehlo()
# #             if SMTP_PORT in (587, 25):
# #                 try:
# #                     smtp.starttls()
# #                     smtp.ehlo()
# #                 except Exception:
# #                     pass
# #             if SMTP_USER and SMTP_PASS:
# #                 smtp.login(SMTP_USER, SMTP_PASS)
# #             smtp.send_message(msg)
# #         logger.info("Email sent to %s", to_address)
# #         return True
# #     except Exception:
# #         logger.exception("Failed to send email to %s", to_address)
# #         return False

# # def send_sms(to_number, message):
# #     """Send SMS via Twilio if configured. Returns True on success."""
# #     if not _TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
# #         logger.warning("Twilio not configured; skipping SMS to %s", to_number)
# #         return False
# #     try:
# #         client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
# #         client.messages.create(body=message, from_=TWILIO_FROM, to=to_number)
# #         logger.info("SMS sent to %s", to_number)
# #         return True
# #     except Exception:
# #         logger.exception("Failed to send SMS to %s", to_number)
# #         return False

# # def notify_async(fn, *args, **kwargs):
# #     """Run notification function in a daemon thread to avoid blocking requests."""
# #     try:
# #         t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
# #         t.start()
# #     except Exception:
# #         logger.exception("Failed to start notification thread")

# # def send_order_notifications(order_id, product, payload):
# #     """
# #     Compose and send notifications for a new order.
# #     - order_id: int
# #     - product: dict (id,title,unit,price,image,...)
# #     - payload: dict (buyer_name,buyer_phone,buyer_email,quantity,address,notes)
# #     """
# #     farmer_subject = f"New order #{order_id} — {product.get('title')}"
# #     farmer_body = (
# #         f"New order received\n\n"
# #         f"Order ID: {order_id}\n"
# #         f"Product: {product.get('title')} (ID: {product.get('id')})\n"
# #         f"Quantity: {payload.get('quantity')}\n"
# #         f"Unit: {product.get('unit','')}\n\n"
# #         f"Buyer name: {payload.get('buyer_name')}\n"
# #         f"Buyer phone: {payload.get('buyer_phone')}\n"
# #         f"Buyer email: {payload.get('buyer_email')}\n"
# #         f"Delivery address: {payload.get('address')}\n"
# #         f"Notes: {payload.get('notes')}\n\n"
# #         f"Please contact the buyer to confirm and arrange delivery.\n"
# #     )

# #     buyer_subject = f"Order received — #{order_id}"
# #     buyer_body = (
# #         f"Thank you {payload.get('buyer_name')},\n\n"
# #         f"We have received your order (ID: {order_id}) for {product.get('title')} x{payload.get('quantity')}.\n"
# #         f"Our team will contact you shortly to confirm details and delivery.\n\n"
# #         f"Order summary:\n"
# #         f"- Product: {product.get('title')}\n"
# #         f"- Quantity: {payload.get('quantity')} {product.get('unit','')}\n"
# #         f"- Delivery address: {payload.get('address')}\n\n"
# #         f"Please wait for confirmation. Thank you for ordering from Farmyard."
# #     )

# #     farmer_sms = f"New order #{order_id}: {product.get('title')} x{payload.get('quantity')}. Buyer: {payload.get('buyer_name')} {payload.get('buyer_phone')}."
# #     buyer_sms = f"Order #{order_id} received. Thank you {payload.get('buyer_name')}. We'll contact you shortly."

# #     # Determine image path (static images folder)
# #     image_path = None
# #     img_name = product.get('image') or ''
# #     if img_name:
# #         static_dir = os.path.join(os.path.dirname(__file__), 'static', STATIC_IMAGE_FOLDER)
# #         candidate = os.path.join(static_dir, img_name)
# #         if os.path.isfile(candidate):
# #             image_path = candidate

# #     # Send farmer email with inline image if available
# #     if FARMER_EMAIL:
# #         notify_async(send_email_with_image, FARMER_EMAIL, farmer_subject, farmer_body, image_path)
# #     else:
# #         logger.warning("FARMER_EMAIL not set; skipping farmer email")

# #     # Farmer SMS
# #     if FARMER_PHONE:
# #         notify_async(send_sms, FARMER_PHONE, farmer_sms)
# #     else:
# #         logger.warning("FARMER_PHONE not set; skipping farmer SMS")

# #     # Buyer notifications
# #     if payload.get('buyer_email'):
# #         notify_async(send_email_with_image, payload.get('buyer_email'), buyer_subject, buyer_body, image_path)
# #     else:
# #         logger.info("Buyer email not provided; skipping buyer email")

# #     if payload.get('buyer_phone'):
# #         notify_async(send_sms, payload.get('buyer_phone'), buyer_sms)
# #     else:
# #         logger.info("Buyer phone not provided; skipping buyer SMS")

# # # --- Data access functions (SQLite, using ? placeholders) ---
# # def fetch_all_products(category=None, q=None):
# #     try:
# #         db = get_db()
# #         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
# #         params = []
# #         where = []
# #         if category and category.lower() not in ('all', ''):
# #             where.append("category = ?")
# #             params.append(category)
# #         if q:
# #             where.append("(title LIKE ? OR excerpt LIKE ?)")
# #             like = f"%{q}%"
# #             params.extend([like, like])
# #         if where:
# #             sql += " WHERE " + " AND ".join(where)
# #         sql += " ORDER BY id DESC"
# #         cur = db.cursor()
# #         cur.execute(sql, params)
# #         rows = cur.fetchall()
# #         cur.close()
# #         return rows_to_json_safe(rows)
# #     except Exception:
# #         logger.exception("DB error in fetch_all_products")
# #     # fallback to in-memory PRODUCTS
# #     results = PRODUCTS.copy()
# #     if category and category.lower() not in ('all', ''):
# #         results = [p for p in results if p.get('category') == category]
# #     if q:
# #         qlow = q.lower()
# #         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
# #     return results

# # def fetch_product_by_id(product_id):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id = ?", (product_id,))
# #         row = cur.fetchone()
# #         cur.close()
# #         if row:
# #             row = dict(row)
# #             if isinstance(row.get('price'), Decimal):
# #                 row['price'] = float(row['price'])
# #             elif isinstance(row.get('price'), (int, float)):
# #                 row['price'] = float(row['price'])
# #             return row
# #         return None
# #     except Exception:
# #         logger.exception("DB error in fetch_product_by_id")
# #     return next((x for x in PRODUCTS if x['id'] == product_id), None)

# # def insert_order(product_id, name, phone, email, quantity, address, notes):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute(
# #             "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (?,?,?,?,?,?,?)",
# #             (product_id, name, phone, email, quantity, address, notes)
# #         )
# #         order_id = cur.lastrowid
# #         db.commit()
# #         cur.close()
# #         return order_id
# #     except Exception:
# #         logger.exception("DB error in insert_order")
# #         try:
# #             db.rollback()
# #         except Exception:
# #             pass
# #     return None

# # @app.context_processor
# # def utility_processor():
# #     def endpoint_exists(name):
# #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# #     return dict(endpoint_exists=endpoint_exists)

# # # --- Routes ---
# # @app.route('/')
# # def index():
# #     return render_template('index.html')

# # @app.route('/shop')
# # def shop():
# #     cat = request.args.get('category')
# #     q = request.args.get('q')
# #     products = fetch_all_products(category=cat, q=q)
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# #         cats = [r['category'] for r in cur.fetchall()]
# #         cur.close()
# #     except Exception:
# #         logger.warning("DB unavailable when fetching categories; using fallback categories")
# #         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
# #     categories = ['all'] + cats
# #     active_category = cat or 'all'
# #     return render_template('shop.html',
# #                            products=products,
# #                            categories=categories,
# #                            active_category=active_category,
# #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/api/product/<int:product_id>')
# # def product_api(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'not found'}), 404
# #     return jsonify(p)

# # @app.route('/product/<int:product_id>')
# # def product_detail(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         abort(404)
# #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# # def order(product_id):
# #     if request.method == 'GET':
# #         return render_template('order_form.html', product_id=product_id)

# #     if request.is_json:
# #         data = request.get_json() or {}
# #     else:
# #         data = request.form.to_dict()

# #     name = (data.get('buyer_name') or '').strip()
# #     phone = (data.get('buyer_phone') or '').strip()
# #     email = (data.get('buyer_email') or '').strip()
# #     try:
# #         quantity = int(data.get('quantity') or 1)
# #     except Exception:
# #         quantity = 1
# #     address = data.get('address') or ''
# #     notes = data.get('notes') or ''

# #     if not name or not phone or quantity < 1:
# #         return jsonify({'error': 'Missing required fields'}), 400

# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'Product not found'}), 404

# #     try:
# #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# #         if not order_id:
# #             app.logger.error("Order insert returned no id (DB may be down)")
# #             return jsonify({'error': 'Server error'}), 500

# #         # Prepare payload and trigger notifications in background
# #         payload = {
# #             'buyer_name': name,
# #             'buyer_phone': phone,
# #             'buyer_email': email,
# #             'quantity': quantity,
# #             'address': address,
# #             'notes': notes
# #         }
# #         notify_async(send_order_notifications, order_id, p, payload)

# #         return jsonify({'success': True, 'order_id': order_id}), 201
# #     except Exception:
# #         app.logger.exception("Order insert failed")
# #         return jsonify({'error': 'Server error'}), 500

# # @app.route('/projects')
# # def projects():
# #     return render_template('projects.html', title="Projects — Farmyard")

# # @app.route('/about')
# # def about():
# #     return render_template('about.html', title="About — Farmyard")

# # @app.route('/contact', methods=['GET','POST'])
# # def contact():
# #     if request.method == 'POST':
# #         name = request.form.get('name')
# #         email = request.form.get('email')
# #         message = request.form.get('message')
# #         if not name or not email or not message:
# #             flash('Please fill in the required fields.', 'danger')
# #             return redirect(url_for('contact'))
# #         flash('Message sent — thank you!', 'success')
# #         return redirect(url_for('contact'))
# #     return render_template('contact.html', title="Contact — Farmyard")

# # @app.route('/privacy')
# # def privacy():
# #     return render_template('privacy.html', title='Privacy Policy')

# # @app.route('/terms')
# # def terms():
# #     return render_template('terms.html', title='Terms of Service')

# # if __name__ == '__main__':
# #     try:
# #         ensure_tables()
# #     except Exception:
# #         logger.exception('ensure_tables failed at startup')
# #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))



# # import os
# # import logging
# # import sqlite3
# # import threading
# # import smtplib
# # from email.message import EmailMessage
# # from decimal import Decimal
# # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect, current_app

# # # --- Configuration via environment variables ---
# # DB_NAME = os.getenv('DB_NAME', os.path.expanduser('~/farmyard.db'))
# # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')

# # # --- Notification configuration (via environment variables) ---
# # SMTP_HOST = os.getenv('SMTP_HOST')  # e.g. smtp.gmail.com or localhost for debug server
# # SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
# # SMTP_USER = os.getenv('SMTP_USER')  # SMTP username (email)
# # SMTP_PASS = os.getenv('SMTP_PASS')  # SMTP password or app password
# # FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USER)
# # FARMER_EMAIL = os.getenv('FARMER_EMAIL')  # farmer's email address

# # TWILIO_SID = os.getenv('TWILIO_ACCOUNT_SID')
# # TWILIO_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
# # TWILIO_FROM = os.getenv('TWILIO_FROM_NUMBER')  # e.g. +1234567890 (Twilio number)
# # FARMER_PHONE = os.getenv('FARMER_PHONE')  # farmer phone number in E.164 e.g. +2547xxxxxxx

# # # Optional: Twilio for SMS
# # try:
# #     from twilio.rest import Client as TwilioClient
# #     _TWILIO_AVAILABLE = True
# # except Exception:
# #     _TWILIO_AVAILABLE = False

# # app = Flask(__name__)
# # app.config['JSON_SORT_KEYS'] = False

# # # --- Fallback sample products (used if DB is unavailable) ---
# # PRODUCTS = [
# #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # ]

# # # configure logger
# # logger = logging.getLogger(__name__)
# # logging.basicConfig(level=logging.INFO)

# # # --- SQLite helpers ---
# # def get_db():
# #     if 'db' in g:
# #         return g.db
# #     db_path = DB_NAME
# #     if not os.path.isabs(db_path):
# #         db_path = os.path.expanduser(db_path)
# #     parent = os.path.dirname(db_path) or '.'
# #     os.makedirs(parent, exist_ok=True)
# #     conn = sqlite3.connect(db_path, check_same_thread=False)
# #     conn.row_factory = sqlite3.Row
# #     g.db = conn
# #     return conn

# # @app.teardown_appcontext
# # def close_db(exc):
# #     db = g.pop('db', None)
# #     if db is not None:
# #         try:
# #             db.close()
# #         except Exception:
# #             pass

# # def rows_to_json_safe(rows):
# #     out = []
# #     for r in rows:
# #         row = dict(r)
# #         price_val = row.get('price')
# #         if isinstance(price_val, Decimal):
# #             row['price'] = float(price_val)
# #         elif isinstance(price_val, (int, float)):
# #             row['price'] = float(price_val)
# #         out.append(row)
# #     return out

# # def ensure_tables():
# #     db = get_db()
# #     cur = db.cursor()
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS products (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           category TEXT NOT NULL,
# #           title TEXT NOT NULL,
# #           excerpt TEXT,
# #           description TEXT,
# #           price INTEGER NOT NULL,
# #           unit TEXT,
# #           image TEXT
# #         );
# #     """)
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS orders (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           product_id INTEGER,
# #           buyer_name TEXT NOT NULL,
# #           buyer_phone TEXT NOT NULL,
# #           buyer_email TEXT,
# #           quantity INTEGER NOT NULL DEFAULT 1,
# #           address TEXT,
# #           notes TEXT,
# #           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# #         );
# #     """)
# #     db.commit()
# #     cur.close()

# # # --- Notifications (email + SMS) ---
# # def send_email(to_address, subject, body_text):
# #     """Send a plain-text email synchronously. Returns True on success."""
# #     try:
# #         if not SMTP_HOST:
# #             logger.warning("SMTP_HOST not configured; skipping email to %s", to_address)
# #             return False
# #         msg = EmailMessage()
# #         msg['From'] = FROM_EMAIL or 'no-reply@example.com'
# #         msg['To'] = to_address
# #         msg['Subject'] = subject
# #         msg.set_content(body_text)
# #         with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
# #             smtp.ehlo()
# #             if SMTP_PORT in (587, 25):
# #                 smtp.starttls()
# #                 smtp.ehlo()
# #             if SMTP_USER and SMTP_PASS:
# #                 smtp.login(SMTP_USER, SMTP_PASS)
# #             smtp.send_message(msg)
# #         logger.info("Email sent to %s", to_address)
# #         return True
# #     except Exception:
# #         logger.exception("Failed to send email to %s", to_address)
# #         return False

# # def send_sms(to_number, message):
# #     """Send SMS via Twilio if configured. Returns True on success."""
# #     if not _TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
# #         logger.warning("Twilio not configured; skipping SMS to %s", to_number)
# #         return False
# #     try:
# #         client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
# #         client.messages.create(body=message, from_=TWILIO_FROM, to=to_number)
# #         logger.info("SMS sent to %s", to_number)
# #         return True
# #     except Exception:
# #         logger.exception("Failed to send SMS to %s", to_number)
# #         return False

# # def notify_async(fn, *args, **kwargs):
# #     """Run notification function in a daemon thread to avoid blocking requests."""
# #     try:
# #         t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
# #         t.start()
# #     except Exception:
# #         logger.exception("Failed to start notification thread")

# # def send_order_notifications(order_id, product, payload):
# #     """
# #     Compose and send notifications for a new order.
# #     - order_id: int
# #     - product: dict (id,title,unit,price,...)
# #     - payload: dict (buyer_name,buyer_phone,buyer_email,quantity,address,notes)
# #     """
# #     farmer_subject = f"New order #{order_id} — {product.get('title')}"
# #     farmer_body = (
# #         f"New order received\n\n"
# #         f"Order ID: {order_id}\n"
# #         f"Product: {product.get('title')} (ID: {product.get('id')})\n"
# #         f"Quantity: {payload.get('quantity')}\n"
# #         f"Unit: {product.get('unit','')}\n\n"
# #         f"Buyer name: {payload.get('buyer_name')}\n"
# #         f"Buyer phone: {payload.get('buyer_phone')}\n"
# #         f"Buyer email: {payload.get('buyer_email')}\n"
# #         f"Delivery address: {payload.get('address')}\n"
# #         f"Notes: {payload.get('notes')}\n\n"
# #         f"Please contact the buyer to confirm and arrange delivery.\n"
# #     )

# #     buyer_subject = f"Order received — #{order_id}"
# #     buyer_body = (
# #         f"Thank you {payload.get('buyer_name')},\n\n"
# #         f"We have received your order (ID: {order_id}) for {product.get('title')} x{payload.get('quantity')}.\n"
# #         f"Our team will contact you shortly to confirm details and delivery.\n\n"
# #         f"Order summary:\n"
# #         f"- Product: {product.get('title')}\n"
# #         f"- Quantity: {payload.get('quantity')} {product.get('unit','')}\n"
# #         f"- Delivery address: {payload.get('address')}\n\n"
# #         f"Please wait for confirmation. Thank you for ordering from Farmyard."
# #     )

# #     farmer_sms = (
# #         f"New order #{order_id}: {product.get('title')} x{payload.get('quantity')}. "
# #         f"Buyer: {payload.get('buyer_name')} {payload.get('buyer_phone')}. Addr: {payload.get('address')}"
# #     )
# #     buyer_sms = f"Order #{order_id} received. Thank you {payload.get('buyer_name')}. We'll contact you shortly."

# #     # Send notifications asynchronously
# #     if FARMER_EMAIL:
# #         notify_async(send_email, FARMER_EMAIL, farmer_subject, farmer_body)
# #     else:
# #         logger.warning("FARMER_EMAIL not set; skipping farmer email")

# #     if FARMER_PHONE:
# #         notify_async(send_sms, FARMER_PHONE, farmer_sms)
# #     else:
# #         logger.warning("FARMER_PHONE not set; skipping farmer SMS")

# #     if payload.get('buyer_email'):
# #         notify_async(send_email, payload.get('buyer_email'), buyer_subject, buyer_body)
# #     else:
# #         logger.info("Buyer email not provided; skipping buyer email")

# #     if payload.get('buyer_phone'):
# #         notify_async(send_sms, payload.get('buyer_phone'), buyer_sms)
# #     else:
# #         logger.info("Buyer phone not provided; skipping buyer SMS")

# # # --- Data access functions (SQLite, using ? placeholders) ---
# # def fetch_all_products(category=None, q=None):
# #     try:
# #         db = get_db()
# #         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
# #         params = []
# #         where = []
# #         if category and category.lower() not in ('all', ''):
# #             where.append("category = ?")
# #             params.append(category)
# #         if q:
# #             where.append("(title LIKE ? OR excerpt LIKE ?)")
# #             like = f"%{q}%"
# #             params.extend([like, like])
# #         if where:
# #             sql += " WHERE " + " AND ".join(where)
# #         sql += " ORDER BY id DESC"
# #         cur = db.cursor()
# #         cur.execute(sql, params)
# #         rows = cur.fetchall()
# #         cur.close()
# #         return rows_to_json_safe(rows)
# #     except Exception:
# #         logger.exception("DB error in fetch_all_products")
# #     # fallback to in-memory PRODUCTS
# #     results = PRODUCTS.copy()
# #     if category and category.lower() not in ('all', ''):
# #         results = [p for p in results if p.get('category') == category]
# #     if q:
# #         qlow = q.lower()
# #         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
# #     return results

# # def fetch_product_by_id(product_id):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id = ?", (product_id,))
# #         row = cur.fetchone()
# #         cur.close()
# #         if row:
# #             row = dict(row)
# #             if isinstance(row.get('price'), Decimal):
# #                 row['price'] = float(row['price'])
# #             elif isinstance(row.get('price'), (int, float)):
# #                 row['price'] = float(row['price'])
# #             return row
# #         return None
# #     except Exception:
# #         logger.exception("DB error in fetch_product_by_id")
# #     return next((x for x in PRODUCTS if x['id'] == product_id), None)

# # def insert_order(product_id, name, phone, email, quantity, address, notes):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute(
# #             "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (?,?,?,?,?,?,?)",
# #             (product_id, name, phone, email, quantity, address, notes)
# #         )
# #         order_id = cur.lastrowid
# #         db.commit()
# #         cur.close()
# #         return order_id
# #     except Exception:
# #         logger.exception("DB error in insert_order")
# #         try:
# #             db.rollback()
# #         except Exception:
# #             pass
# #     return None

# # @app.context_processor
# # def utility_processor():
# #     def endpoint_exists(name):
# #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# #     return dict(endpoint_exists=endpoint_exists)

# # # --- Routes ---
# # @app.route('/')
# # def index():
# #     return render_template('index.html')

# # @app.route('/shop')
# # def shop():
# #     cat = request.args.get('category')
# #     q = request.args.get('q')
# #     products = fetch_all_products(category=cat, q=q)
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# #         cats = [r['category'] for r in cur.fetchall()]
# #         cur.close()
# #     except Exception:
# #         logger.warning("DB unavailable when fetching categories; using fallback categories")
# #         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
# #     categories = ['all'] + cats
# #     active_category = cat or 'all'
# #     return render_template('shop.html',
# #                            products=products,
# #                            categories=categories,
# #                            active_category=active_category,
# #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/api/product/<int:product_id>')
# # def product_api(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'not found'}), 404
# #     return jsonify(p)

# # @app.route('/product/<int:product_id>')
# # def product_detail(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         abort(404)
# #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# # def order(product_id):
# #     if request.method == 'GET':
# #         return render_template('order_form.html', product_id=product_id)

# #     if request.is_json:
# #         data = request.get_json() or {}
# #     else:
# #         data = request.form.to_dict()

# #     name = (data.get('buyer_name') or '').strip()
# #     phone = (data.get('buyer_phone') or '').strip()
# #     email = (data.get('buyer_email') or '').strip()
# #     try:
# #         quantity = int(data.get('quantity') or 1)
# #     except Exception:
# #         quantity = 1
# #     address = data.get('address') or ''
# #     notes = data.get('notes') or ''

# #     if not name or not phone or quantity < 1:
# #         return jsonify({'error': 'Missing required fields'}), 400

# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'Product not found'}), 404

# #     try:
# #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# #         if not order_id:
# #             app.logger.error("Order insert returned no id (DB may be down)")
# #             return jsonify({'error': 'Server error'}), 500

# #         # Prepare payload and trigger notifications in background
# #         payload = {
# #             'buyer_name': name,
# #             'buyer_phone': phone,
# #             'buyer_email': email,
# #             'quantity': quantity,
# #             'address': address,
# #             'notes': notes
# #         }
# #         notify_async(send_order_notifications, order_id, p, payload)

# #         return jsonify({'success': True, 'order_id': order_id}), 201
# #     except Exception:
# #         app.logger.exception("Order insert failed")
# #         return jsonify({'error': 'Server error'}), 500

# # @app.route('/projects')
# # def projects():
# #     return render_template('projects.html', title="Projects — Farmyard")

# # @app.route('/about')
# # def about():
# #     return render_template('about.html', title="About — Farmyard")

# # @app.route('/contact', methods=['GET','POST'])
# # def contact():
# #     if request.method == 'POST':
# #         name = request.form.get('name')
# #         email = request.form.get('email')
# #         message = request.form.get('message')
# #         if not name or not email or not message:
# #             flash('Please fill in the required fields.', 'danger')
# #             return redirect(url_for('contact'))
# #         flash('Message sent — thank you!', 'success')
# #         return redirect(url_for('contact'))
# #     return render_template('contact.html', title="Contact — Farmyard")

# # @app.route('/privacy')
# # def privacy():
# #     return render_template('privacy.html', title='Privacy Policy')

# # @app.route('/terms')
# # def terms():
# #     return render_template('terms.html', title='Terms of Service')

# # if __name__ == '__main__':
# #     try:
# #         ensure_tables()
# #     except Exception:
# #         logger.exception('ensure_tables failed at startup')
# #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))




# # import os
# # import logging
# # import sqlite3
# # from decimal import Decimal
# # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect, current_app

# # # --- Configuration via environment variables ---
# # DB_NAME = os.getenv('DB_NAME', os.path.expanduser('~/farmyard.db'))
# # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')

# # app = Flask(__name__)
# # app.config['JSON_SORT_KEYS'] = False

# # # --- Fallback sample products (used if DB is unavailable) ---
# # PRODUCTS = [
# #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # ]

# # # configure logger
# # logger = logging.getLogger(__name__)
# # logging.basicConfig(level=logging.INFO)

# # # --- SQLite helpers ---
# # def get_db():
# #     if 'db' in g:
# #         return g.db
# #     db_path = DB_NAME
# #     if not os.path.isabs(db_path):
# #         db_path = os.path.expanduser(db_path)
# #     # ensure parent directory exists
# #     parent = os.path.dirname(db_path) or '.'
# #     os.makedirs(parent, exist_ok=True)
# #     conn = sqlite3.connect(db_path, check_same_thread=False)
# #     conn.row_factory = sqlite3.Row
# #     g.db = conn
# #     return conn

# # @app.teardown_appcontext
# # def close_db(exc):
# #     db = g.pop('db', None)
# #     if db is not None:
# #         try:
# #             db.close()
# #         except Exception:
# #             pass

# # def rows_to_json_safe(rows):
# #     out = []
# #     for r in rows:
# #         row = dict(r)
# #         price_val = row.get('price')
# #         if isinstance(price_val, Decimal):
# #             row['price'] = float(price_val)
# #         elif isinstance(price_val, (int, float)):
# #             row['price'] = float(price_val)
# #         out.append(row)
# #     return out

# # def ensure_tables():
# #     db = get_db()
# #     cur = db.cursor()
# #     # products table: create only if missing (your DB already has it)
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS products (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           category TEXT NOT NULL,
# #           title TEXT NOT NULL,
# #           excerpt TEXT,
# #           description TEXT,
# #           price INTEGER NOT NULL,
# #           unit TEXT,
# #           image TEXT
# #         );
# #     """)
# #     # orders table required by the app
# #     cur.execute("""
# #         CREATE TABLE IF NOT EXISTS orders (
# #           id INTEGER PRIMARY KEY AUTOINCREMENT,
# #           product_id INTEGER,
# #           buyer_name TEXT NOT NULL,
# #           buyer_phone TEXT NOT NULL,
# #           buyer_email TEXT,
# #           quantity INTEGER NOT NULL DEFAULT 1,
# #           address TEXT,
# #           notes TEXT,
# #           created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# #         );
# #     """)
# #     db.commit()
# #     cur.close()

# # # --- Data access functions (SQLite, using ? placeholders) ---
# # def fetch_all_products(category=None, q=None):
# #     try:
# #         db = get_db()
# #         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
# #         params = []
# #         where = []
# #         if category and category.lower() not in ('all', ''):
# #             where.append("category = ?")
# #             params.append(category)
# #         if q:
# #             where.append("(title LIKE ? OR excerpt LIKE ?)")
# #             like = f"%{q}%"
# #             params.extend([like, like])
# #         if where:
# #             sql += " WHERE " + " AND ".join(where)
# #         sql += " ORDER BY id DESC"
# #         cur = db.cursor()
# #         cur.execute(sql, params)
# #         rows = cur.fetchall()
# #         cur.close()
# #         return rows_to_json_safe(rows)
# #     except Exception:
# #         logger.exception("DB error in fetch_all_products")
# #     # fallback to in-memory PRODUCTS
# #     results = PRODUCTS.copy()
# #     if category and category.lower() not in ('all', ''):
# #         results = [p for p in results if p.get('category') == category]
# #     if q:
# #         qlow = q.lower()
# #         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
# #     return results

# # def fetch_product_by_id(product_id):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id = ?", (product_id,))
# #         row = cur.fetchone()
# #         cur.close()
# #         if row:
# #             row = dict(row)
# #             if isinstance(row.get('price'), Decimal):
# #                 row['price'] = float(row['price'])
# #             elif isinstance(row.get('price'), (int, float)):
# #                 row['price'] = float(row['price'])
# #             return row
# #         return None
# #     except Exception:
# #         logger.exception("DB error in fetch_product_by_id")
# #     return next((x for x in PRODUCTS if x['id'] == product_id), None)

# # def insert_order(product_id, name, phone, email, quantity, address, notes):
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute(
# #             "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (?,?,?,?,?,?,?)",
# #             (product_id, name, phone, email, quantity, address, notes)
# #         )
# #         order_id = cur.lastrowid
# #         db.commit()
# #         cur.close()
# #         return order_id
# #     except Exception:
# #         logger.exception("DB error in insert_order")
# #         try:
# #             db.rollback()
# #         except Exception:
# #             pass
# #     return None

# # @app.context_processor
# # def utility_processor():
# #     def endpoint_exists(name):
# #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# #     return dict(endpoint_exists=endpoint_exists)

# # # --- Routes ---
# # @app.route('/')
# # def index():
# #     return render_template('index.html')

# # @app.route('/shop')
# # def shop():
# #     cat = request.args.get('category')
# #     q = request.args.get('q')
# #     products = fetch_all_products(category=cat, q=q)
# #     try:
# #         db = get_db()
# #         cur = db.cursor()
# #         cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# #         cats = [r['category'] for r in cur.fetchall()]
# #         cur.close()
# #     except Exception:
# #         logger.warning("DB unavailable when fetching categories; using fallback categories")
# #         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
# #     categories = ['all'] + cats
# #     active_category = cat or 'all'
# #     return render_template('shop.html',
# #                            products=products,
# #                            categories=categories,
# #                            active_category=active_category,
# #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/api/product/<int:product_id>')
# # def product_api(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'not found'}), 404
# #     return jsonify(p)

# # @app.route('/product/<int:product_id>')
# # def product_detail(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         abort(404)
# #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# # def order(product_id):
# #     if request.method == 'GET':
# #         return render_template('order_form.html', product_id=product_id)
# #     if request.is_json:
# #         data = request.get_json() or {}
# #     else:
# #         data = request.form.to_dict()
# #     name = (data.get('buyer_name') or '').strip()
# #     phone = (data.get('buyer_phone') or '').strip()
# #     email = (data.get('buyer_email') or '').strip()
# #     try:
# #         quantity = int(data.get('quantity') or 1)
# #     except Exception:
# #         quantity = 1
# #     address = data.get('address') or ''
# #     notes = data.get('notes') or ''
# #     if not name or not phone or quantity < 1:
# #         return jsonify({'error': 'Missing required fields'}), 400
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'Product not found'}), 404
# #     try:
# #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# #         if not order_id:
# #             app.logger.error("Order insert returned no id (DB may be down)")
# #             return jsonify({'error': 'Server error'}), 500
# #         return jsonify({'success': True, 'order_id': order_id}), 201
# #     except Exception:
# #         app.logger.exception("Order insert failed")
# #         return jsonify({'error': 'Server error'}), 500

# # @app.route('/projects')
# # def projects():
# #     return render_template('projects.html', title="Projects — Farmyard")

# # @app.route('/about')
# # def about():
# #     return render_template('about.html', title="About — Farmyard")

# # @app.route('/contact', methods=['GET','POST'])
# # def contact():
# #     if request.method == 'POST':
# #         name = request.form.get('name')
# #         email = request.form.get('email')
# #         message = request.form.get('message')
# #         if not name or not email or not message:
# #             flash('Please fill in the required fields.', 'danger')
# #             return redirect(url_for('contact'))
# #         flash('Message sent — thank you!', 'success')
# #         return redirect(url_for('contact'))
# #     return render_template('contact.html', title="Contact — Farmyard")

# # @app.route('/privacy')
# # def privacy():
# #     return render_template('privacy.html', title='Privacy Policy')

# # @app.route('/terms')
# # def terms():
# #     return render_template('terms.html', title='Terms of Service')

# # if __name__ == '__main__':
# #     try:
# #         ensure_tables()
# #     except Exception:
# #         logger.exception('ensure_tables failed at startup')
# #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))



# # import os
# # import logging
# # import sqlite3
# # import pymysql
# # from pymysql import OperationalError
# # from pymysql.cursors import DictCursor
# # from flask import current_app
# # from decimal import Decimal
# # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect

# # # --- Configuration via environment variables ---
# # DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
# # DB_USER = os.getenv('DB_USER', 'shop_user')
# # DB_PASS = os.getenv('DB_PASS', 'user')
# # DB_NAME = os.getenv('DB_NAME', 'farmyard.db')  # default to sqlite file
# # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')  # static/<folder> where product images live

# # app = Flask(__name__)
# # app.config['JSON_SORT_KEYS'] = False

# # # --- Fallback sample products (used if DB is unavailable) ---
# # PRODUCTS = [
# #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # ]

# # # configure logger
# # logger = logging.getLogger(__name__)
# # logging.basicConfig(level=logging.INFO)

# # # --- DB helpers (SQLite-aware adapter that accepts %s placeholders) ---
# # def get_db():
# #     """
# #     Return a DB connection. If DB_NAME ends with .db we use SQLite (file path).
# #     The SQLite cursor wrapper converts %s placeholders to ? so existing queries work.
# #     """
# #     if 'db' in g:
# #         return g.db

# #     # Use SQLite when DB_NAME looks like a file (endswith .db)
# #     if isinstance(DB_NAME, str) and DB_NAME.lower().endswith('.db'):
# #         db_path = DB_NAME
# #         if not os.path.isabs(db_path):
# #             db_path = os.path.join(os.path.expanduser('~'), db_path)
# #         os.makedirs(os.path.dirname(db_path), exist_ok=True)
# #         conn = sqlite3.connect(db_path, check_same_thread=False)
# #         conn.row_factory = sqlite3.Row

# #         class CursorWrapper:
# #             def __init__(self, cur):
# #                 self._cur = cur

# #             def execute(self, sql, params=()):
# #                 # convert MySQL-style %s placeholders to sqlite ? placeholders
# #                 sql2 = sql.replace('%s', '?')
# #                 return self._cur.execute(sql2, params or ())

# #             def executemany(self, sql, seq_of_params):
# #                 sql2 = sql.replace('%s', '?')
# #                 return self._cur.executemany(sql2, seq_of_params)

# #             def fetchall(self):
# #                 rows = self._cur.fetchall()
# #                 return [dict(r) for r in rows]

# #             def fetchone(self):
# #                 r = self._cur.fetchone()
# #                 return dict(r) if r else None

# #             @property
# #             def lastrowid(self):
# #                 try:
# #                     return self._cur.lastrowid
# #                 except AttributeError:
# #                     # sqlite3 cursor exposes lastrowid on cursor object after execute
# #                     return None

# #             def close(self):
# #                 try:
# #                     self._cur.close()
# #                 except Exception:
# #                     pass

# #             def __enter__(self):
# #                 return self

# #             def __exit__(self, exc_type, exc, tb):
# #                 try:
# #                     self._cur.close()
# #                 except Exception:
# #                     pass

# #         def cursor_factory():
# #             return CursorWrapper(conn.cursor())

# #         # monkeypatch a .cursor method on the connection object
# #         conn.cursor = cursor_factory
# #         g.db = conn
# #         return g.db

# #     # default: use pymysql as before
# #     g.db = pymysql.connect(
# #         host=DB_HOST,
# #         user=DB_USER,
# #         password=DB_PASS,
# #         db=DB_NAME,
# #         charset='utf8mb4',
# #         cursorclass=DictCursor,
# #         autocommit=False
# #     )
# #     return g.db

# # @app.teardown_appcontext
# # def close_db(exc):
# #     db = g.pop('db', None)
# #     if db is not None:
# #         try:
# #             db.close()
# #         except Exception:
# #             pass

# # def rows_to_json_safe(rows):
# #     out = []
# #     for r in rows:
# #         row = dict(r)
# #         price_val = row.get('price')
# #         if isinstance(price_val, Decimal):
# #             row['price'] = float(price_val)
# #         elif isinstance(price_val, (int,)):
# #             # SQLite stores price as INTEGER in your schema; convert to float for JSON/templates
# #             row['price'] = float(price_val)
# #         out.append(row)
# #     return out

# # # --- Data access functions (compatible with your products schema) ---
# # def fetch_all_products(category=None, q=None):
# #     """
# #     Fetch products using the columns present in your SQLite schema.
# #     Works with MySQL or SQLite (via get_db wrapper).
# #     """
# #     try:
# #         db = get_db()
# #         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
# #         params = []
# #         where = []
# #         if category and category.lower() not in ('all', ''):
# #             where.append("category = %s")
# #             params.append(category)
# #         if q:
# #             where.append("(title LIKE %s OR excerpt LIKE %s)")
# #             like = f"%{q}%"
# #             params.extend([like, like])
# #         if where:
# #             sql += " WHERE " + " AND ".join(where)
# #         sql += " ORDER BY id DESC"
# #         with db.cursor() as cur:
# #             cur.execute(sql, params)
# #             rows = cur.fetchall()
# #         return rows_to_json_safe(rows)
# #     except OperationalError as e:
# #         logger.warning("MySQL OperationalError in fetch_all_products: %s", e)
# #     except Exception:
# #         logger.exception("DB error in fetch_all_products")
# #     # fallback: filter PRODUCTS list
# #     results = PRODUCTS.copy()
# #     if category and category.lower() not in ('all', ''):
# #         results = [p for p in results if p.get('category') == category]
# #     if q:
# #         qlow = q.lower()
# #         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
# #     return results

# # def fetch_product_by_id(product_id):
# #     try:
# #         db = get_db()
# #         with db.cursor() as cur:
# #             cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id=%s", (product_id,))
# #             row = cur.fetchone()
# #         if row and isinstance(row.get('price'), Decimal):
# #             row['price'] = float(row['price'])
# #         return row
# #     except OperationalError as e:
# #         logger.warning("MySQL OperationalError in fetch_product_by_id: %s", e)
# #     except Exception:
# #         logger.exception("DB error in fetch_product_by_id")
# #     # fallback: find in PRODUCTS
# #     p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# #     return p

# # def insert_order(product_id, name, phone, email, quantity, address, notes):
# #     try:
# #         db = get_db()
# #         with db.cursor() as cur:
# #             cur.execute(
# #                 "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) "
# #                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
# #                 (product_id, name, phone, email, quantity, address, notes)
# #             )
# #             # for sqlite wrapper, lastrowid may be None on wrapper; try to fetch from cursor if available
# #             order_id = getattr(cur, 'lastrowid', None)
# #             if not order_id:
# #                 # try to get last inserted id via connection (sqlite)
# #                 try:
# #                     if isinstance(db, sqlite3.Connection):
# #                         order_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
# #                 except Exception:
# #                     order_id = None
# #         db.commit()
# #         return order_id
# #     except OperationalError as e:
# #         logger.warning("MySQL OperationalError in insert_order: %s", e)
# #     except Exception:
# #         logger.exception("DB error in insert_order")
# #     return None

# # # --- Utility to ensure orders table exists for SQLite deployments ---
# # def ensure_orders_table():
# #     if isinstance(DB_NAME, str) and DB_NAME.lower().endswith('.db'):
# #         db = get_db()
# #         try:
# #             # Use sqlite-compatible SQL (works with wrapper because %s -> ?)
# #             with db.cursor() as cur:
# #                 cur.execute("""
# #                     CREATE TABLE IF NOT EXISTS orders (
# #                         id INTEGER PRIMARY KEY AUTOINCREMENT,
# #                         product_id INTEGER,
# #                         buyer_name TEXT NOT NULL,
# #                         buyer_phone TEXT NOT NULL,
# #                         buyer_email TEXT,
# #                         quantity INTEGER NOT NULL DEFAULT 1,
# #                         address TEXT,
# #                         notes TEXT,
# #                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
# #                     )
# #                 """, ())
# #             db.commit()
# #         except Exception:
# #             logger.exception("Failed to ensure orders table exists")

# # @app.context_processor
# # def utility_processor():
# #     def endpoint_exists(name):
# #         """
# #         Return True if an endpoint with the given name is registered.
# #         Works for plain view functions and blueprint endpoints (use 'bp.view').
# #         """
# #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# #     return dict(endpoint_exists=endpoint_exists)

# # # --- Routes ---
# # @app.route('/')
# # def index():
# #     return render_template('index.html')

# # @app.route('/shop')
# # def shop():
# #     cat = request.args.get('category')
# #     q = request.args.get('q')
# #     products = fetch_all_products(category=cat, q=q)

# #     # build categories list from DB (distinct) with fallback
# #     try:
# #         db = get_db()
# #         with db.cursor() as cur:
# #             cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# #             cats = [r['category'] for r in cur.fetchall()]
# #     except Exception:
# #         logger.warning("DB unavailable when fetching categories; using fallback categories")
# #         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
# #     categories = ['all'] + cats
# #     active_category = cat or 'all'
# #     return render_template('shop.html',
# #                            products=products,
# #                            categories=categories,
# #                            active_category=active_category,
# #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # @app.route('/api/product/<int:product_id>')
# # def product_api(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'not found'}), 404
# #     return jsonify(p)

# # @app.route('/product/<int:product_id>')
# # def product_detail(product_id):
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         abort(404)
# #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)


# # @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# # def order(product_id):
# #     # GET: render the HTML order form for browser testing
# #     if request.method == 'GET':
# #         return render_template('order_form.html', product_id=product_id)

# #     # POST: accept JSON or form-encoded data
# #     if request.is_json:
# #         data = request.get_json() or {}
# #     else:
# #         data = request.form.to_dict()

# #     name = (data.get('buyer_name') or '').strip()
# #     phone = (data.get('buyer_phone') or '').strip()
# #     email = (data.get('buyer_email') or '').strip()
# #     try:
# #         quantity = int(data.get('quantity') or 1)
# #     except Exception:
# #         quantity = 1
# #     address = data.get('address') or ''
# #     notes = data.get('notes') or ''

# #     if not name or not phone or quantity < 1:
# #         return jsonify({'error': 'Missing required fields'}), 400

# #     # ensure product exists (fetch_product_by_id has fallback)
# #     p = fetch_product_by_id(product_id)
# #     if not p:
# #         return jsonify({'error': 'Product not found'}), 404

# #     try:
# #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# #         if not order_id:
# #             app.logger.error("Order insert returned no id (DB may be down)")
# #             return jsonify({'error': 'Server error'}), 500
# #         return jsonify({'success': True, 'order_id': order_id}), 201
# #     except Exception:
# #         app.logger.exception("Order insert failed")
# #         return jsonify({'error': 'Server error'}), 500

# # @app.route('/projects')
# # def projects():
# #     return render_template('projects.html', title="Projects — Farmyard")

# # @app.route('/about')
# # def about():
# #     return render_template('about.html', title="About — Farmyard")

# # @app.route('/contact', methods=['GET','POST'])
# # def contact():
# #     if request.method == 'POST':
# #         name = request.form.get('name')
# #         email = request.form.get('email')
# #         message = request.form.get('message')
# #         if not name or not email or not message:
# #             flash('Please fill in the required fields.', 'danger')
# #             return redirect(url_for('contact'))
# #         flash('Message sent — thank you!', 'success')
# #         return redirect(url_for('contact'))
# #     return render_template('contact.html', title="Contact — Farmyard")

# # @app.route('/privacy')
# # def privacy():
# #     return render_template('privacy.html', title='Privacy Policy')

# # @app.route('/terms')
# # def terms():
# #     return render_template('terms.html', title='Terms of Service')

# # # --- Run ---
# # if __name__ == '__main__':
# #     # ensure orders table exists when using SQLite
# #     try:
# #         ensure_orders_table()
# #     except Exception:
# #         logger.exception("ensure_orders_table failed at startup")
# #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))






# # # import os
# # # import logging
# # # import pymysql
# # # from pymysql import OperationalError
# # # from pymysql.cursors import DictCursor
# # # from flask import current_app
# # # from decimal import Decimal
# # # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect

# # # # --- Configuration via environment variables ---
# # # DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
# # # DB_USER = os.getenv('DB_USER', 'shop_user')
# # # DB_PASS = os.getenv('DB_PASS', 'user')
# # # DB_NAME = os.getenv('DB_NAME', 'shop')
# # # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')  # static/<folder> where product images live

# # # app = Flask(__name__)
# # # app.config['JSON_SORT_KEYS'] = False

# # # # --- Fallback sample products (used if MySQL is unavailable) ---
# # # PRODUCTS = [
# # #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# # #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# # #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# # #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# # #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# # #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # # ]

# # # # configure logger
# # # logger = logging.getLogger(__name__)
# # # logging.basicConfig(level=logging.INFO)

# # # # --- DB helpers ---
# # # import sqlite3

# # # def get_db():
# # #     """
# # #     Return a DB connection. If DB_NAME ends with .db we use SQLite (file path).
# # #     The SQLite cursor wrapper converts %s placeholders to ? so existing queries work.
# # #     """
# # #     if 'db' in g:
# # #         return g.db

# # #     # Use SQLite when DB_NAME looks like a file (endswith .db)
# # #     if isinstance(DB_NAME, str) and DB_NAME.lower().endswith('.db'):
# # #         db_path = DB_NAME
# # #         if not os.path.isabs(db_path):
# # #             db_path = os.path.join(os.path.expanduser('~'), db_path)
# # #         os.makedirs(os.path.dirname(db_path), exist_ok=True)
# # #         conn = sqlite3.connect(db_path, check_same_thread=False)
# # #         conn.row_factory = sqlite3.Row

# # #         class CursorWrapper:
# # #             def __init__(self, cur):
# # #                 self._cur = cur

# # #             def execute(self, sql, params=()):
# # #                 sql2 = sql.replace('%s', '?')
# # #                 return self._cur.execute(sql2, params or ())

# # #             def executemany(self, sql, seq_of_params):
# # #                 sql2 = sql.replace('%s', '?')
# # #                 return self._cur.executemany(sql2, seq_of_params)

# # #             def fetchall(self):
# # #                 rows = self._cur.fetchall()
# # #                 return [dict(r) for r in rows]

# # #             def fetchone(self):
# # #                 r = self._cur.fetchone()
# # #                 return dict(r) if r else None

# # #             @property
# # #             def lastrowid(self):
# # #                 try:
# # #                     return self._cur.lastrowid
# # #                 except AttributeError:
# # #                     return None

# # #             def __enter__(self):
# # #                 return self

# # #             def __exit__(self, exc_type, exc, tb):
# # #                 try:
# # #                     self._cur.close()
# # #                 except Exception:
# # #                     pass

# # #         def cursor_factory():
# # #             return CursorWrapper(conn.cursor())

# # #         conn.cursor = cursor_factory
# # #         g.db = conn
# # #         return g.db

# # #     # default: use pymysql as before
# # #     g.db = pymysql.connect(
# # #         host=DB_HOST,
# # #         user=DB_USER,
# # #         password=DB_PASS,
# # #         db=DB_NAME,
# # #         charset='utf8mb4',
# # #         cursorclass=DictCursor,
# # #         autocommit=False
# # #     )
# # #     return g.db

# # # @app.teardown_appcontext
# # # def close_db(exc):
# # #     db = g.pop('db', None)
# # #     if db is not None:
# # #         try:
# # #             db.close()
# # #         except Exception:
# # #             pass


# # # def rows_to_json_safe(rows):
# # #     out = []
# # #     for r in rows:
# # #         row = dict(r)
# # #         price_val = row.get('price')
# # #         if isinstance(price_val, Decimal):
# # #             row['price'] = float(price_val)
# # #         elif isinstance(price_val, (int,)):
# # #             # SQLite stores price as INTEGER in your schema; convert to float for JSON/templates
# # #             row['price'] = float(price_val)
# # #         out.append(row)
# # #     return out


# # # def fetch_all_products(category=None, q=None):
# # #     """
# # #     Fetch products using the columns present in your SQLite schema.
# # #     Works with MySQL or SQLite (via get_db wrapper).
# # #     """
# # #     try:
# # #         db = get_db()
# # #         sql = "SELECT id, category, title, excerpt, description, price, image, unit FROM products"
# # #         params = []
# # #         where = []
# # #         if category and category.lower() not in ('all', ''):
# # #             where.append("category = %s")
# # #             params.append(category)
# # #         if q:
# # #             where.append("(title LIKE %s OR excerpt LIKE %s)")
# # #             like = f"%{q}%"
# # #             params.extend([like, like])
# # #         if where:
# # #             sql += " WHERE " + " AND ".join(where)
# # #         sql += " ORDER BY id DESC"
# # #         with db.cursor() as cur:
# # #             cur.execute(sql, params)
# # #             rows = cur.fetchall()
# # #         return rows_to_json_safe(rows)
# # #     except OperationalError as e:
# # #         logger.warning("MySQL OperationalError in fetch_all_products: %s", e)
# # #     except Exception:
# # #         logger.exception("DB error in fetch_all_products")
# # #     # fallback: filter PRODUCTS list
# # #     results = PRODUCTS.copy()
# # #     if category and category.lower() not in ('all', ''):
# # #         results = [p for p in results if p.get('category') == category]
# # #     if q:
# # #         qlow = q.lower()
# # #         results = [p for p in results if qlow in p.get('title', '').lower() or qlow in p.get('excerpt', '').lower()]
# # #     return results


# # # def fetch_product_by_id(product_id):
# # #     try:
# # #         db = get_db()
# # #         with db.cursor() as cur:
# # #             cur.execute("SELECT id, category, title, excerpt, description, price, image, unit FROM products WHERE id=%s", (product_id,))
# # #             row = cur.fetchone()
# # #         if row and isinstance(row.get('price'), Decimal):
# # #             row['price'] = float(row['price'])
# # #         return row
# # #     except OperationalError as e:
# # #         logger.warning("MySQL OperationalError in fetch_product_by_id: %s", e)
# # #     except Exception:
# # #         logger.exception("DB error in fetch_product_by_id")
# # #     # fallback: find in PRODUCTS
# # #     p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # #     return p

# # # # def get_db():
# # # #     if 'db' not in g:
# # # #         g.db = pymysql.connect(
# # # #             host=DB_HOST,
# # # #             user=DB_USER,
# # # #             password=DB_PASS,
# # # #             db=DB_NAME,
# # # #             charset='utf8mb4',
# # # #             cursorclass=DictCursor,
# # # #             autocommit=False
# # # #         )
# # # #     return g.db

# # # # @app.teardown_appcontext
# # # # def close_db(exc):
# # # #     db = g.pop('db', None)
# # # #     if db is not None:
# # # #         try:
# # # #             db.close()
# # # #         except Exception:
# # # #             pass

# # # # def rows_to_json_safe(rows):
# # # #     out = []
# # # #     for r in rows:
# # # #         row = dict(r)
# # # #         # convert Decimal to float for JSON/template formatting
# # # #         if isinstance(row.get('price'), Decimal):
# # # #             row['price'] = float(row['price'])
# # # #         out.append(row)
# # # #     return out

# # # # # --- Data access functions (safe with fallback) ---
# # # # def fetch_all_products(category=None, q=None):
# # # #     """
# # # #     Try to fetch from MySQL; on any DB error return filtered PRODUCTS fallback.
# # # #     """
# # # #     try:
# # # #         db = get_db()
# # # #         sql = "SELECT id, sku, title, excerpt, description, price, image, category, unit FROM products"
# # # #         params = []
# # # #         where = []
# # # #         if category and category.lower() not in ('all',''):
# # # #             where.append("category = %s")
# # # #             params.append(category)
# # # #         if q:
# # # #             where.append("(title LIKE %s OR excerpt LIKE %s)")
# # # #             like = f"%{q}%"
# # # #             params.extend([like, like])
# # # #         if where:
# # # #             sql += " WHERE " + " AND ".join(where)
# # # #         sql += " ORDER BY created_at DESC"
# # # #         with db.cursor() as cur:
# # # #             cur.execute(sql, params)
# # # #             rows = cur.fetchall()
# # # #         return rows_to_json_safe(rows)
# # # #     except OperationalError as e:
# # # #         logger.warning("MySQL OperationalError in fetch_all_products: %s", e)
# # # #     except Exception:
# # # #         logger.exception("DB error in fetch_all_products")
# # # #     # fallback: filter PRODUCTS list
# # # #     results = PRODUCTS.copy()
# # # #     if category and category.lower() not in ('all',''):
# # # #         results = [p for p in results if p.get('category') == category]
# # # #     if q:
# # # #         qlow = q.lower()
# # # #         results = [p for p in results if qlow in p.get('title','').lower() or qlow in p.get('excerpt','').lower()]
# # # #     return results

# # # # def fetch_product_by_id(product_id):
# # # #     try:
# # # #         db = get_db()
# # # #         with db.cursor() as cur:
# # # #             cur.execute("SELECT id, sku, title, excerpt, description, price, image, category, unit FROM products WHERE id=%s", (product_id,))
# # # #             row = cur.fetchone()
# # # #         if row and isinstance(row.get('price'), Decimal):
# # # #             row['price'] = float(row['price'])
# # # #         return row
# # # #     except OperationalError as e:
# # # #         logger.warning("MySQL OperationalError in fetch_product_by_id: %s", e)
# # # #     except Exception:
# # # #         logger.exception("DB error in fetch_product_by_id")
# # # #     # fallback: find in PRODUCTS
# # # #     p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # #     return p

# # # # def insert_order(product_id, name, phone, email, quantity, address, notes):
# # # #     """
# # # #     Attempt to insert order into MySQL. If DB is unavailable, raise so caller returns 500.
# # # #     (You could also store orders in a queue/file for later processing.)
# # # #     """
# # # #     db = get_db()
# # # #     with db.cursor() as cur:
# # # #         cur.execute("INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
# # # #                     (product_id, name, phone, email, quantity, address, notes))
# # # #         order_id = cur.lastrowid
# # # #     db.commit()
# # # #     return order_id

# # # @app.context_processor
# # # def utility_processor():
# # #     def endpoint_exists(name):
# # #         """
# # #         Return True if an endpoint with the given name is registered.
# # #         Works for plain view functions and blueprint endpoints (use 'bp.view').
# # #         """
# # #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# # #     return dict(endpoint_exists=endpoint_exists)

# # # # --- Routes ---
# # # @app.route('/')
# # # def index():
# # #     return render_template('index.html')

# # # @app.route('/shop')
# # # def shop():
# # #     cat = request.args.get('category')
# # #     q = request.args.get('q')
# # #     products = fetch_all_products(category=cat, q=q)

# # #     # build categories list from DB (distinct) with fallback
# # #     try:
# # #         db = get_db()
# # #         with db.cursor() as cur:
# # #             cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# # #             cats = [r['category'] for r in cur.fetchall()]
# # #     except Exception:
# # #         logger.warning("DB unavailable when fetching categories; using fallback categories")
# # #         cats = sorted({p['category'] for p in PRODUCTS if p.get('category')})
# # #     categories = ['all'] + cats
# # #     active_category = cat or 'all'
# # #     return render_template('shop.html',
# # #                            products=products,
# # #                            categories=categories,
# # #                            active_category=active_category,
# # #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # # @app.route('/api/product/<int:product_id>')
# # # def product_api(product_id):
# # #     p = fetch_product_by_id(product_id)
# # #     if not p:
# # #         return jsonify({'error': 'not found'}), 404
# # #     return jsonify(p)

# # # @app.route('/product/<int:product_id>')
# # # def product_detail(product_id):
# # #     p = fetch_product_by_id(product_id)
# # #     if not p:
# # #         abort(404)
# # #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)


# # # @app.route('/order/<int:product_id>', methods=['GET', 'POST'])
# # # def order(product_id):
# # #     # GET: render the HTML order form for browser testing
# # #     if request.method == 'GET':
# # #         return render_template('order_form.html', product_id=product_id)

# # #     # POST: accept JSON or form-encoded data
# # #     if request.is_json:
# # #         data = request.get_json() or {}
# # #     else:
# # #         data = request.form.to_dict()

# # #     name = (data.get('buyer_name') or '').strip()
# # #     phone = (data.get('buyer_phone') or '').strip()
# # #     email = (data.get('buyer_email') or '').strip()
# # #     try:
# # #         quantity = int(data.get('quantity') or 1)
# # #     except Exception:
# # #         quantity = 1
# # #     address = data.get('address') or ''
# # #     notes = data.get('notes') or ''

# # #     if not name or not phone or quantity < 1:
# # #         return jsonify({'error': 'Missing required fields'}), 400

# # #     # ensure product exists (fetch_product_by_id has fallback)
# # #     p = fetch_product_by_id(product_id)
# # #     if not p:
# # #         return jsonify({'error': 'Product not found'}), 404

# # #     try:
# # #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# # #         if not order_id:
# # #             app.logger.error("Order insert returned no id (DB may be down)")
# # #             return jsonify({'error': 'Server error'}), 500
# # #         return jsonify({'success': True, 'order_id': order_id}), 201
# # #     except Exception:
# # #         app.logger.exception("Order insert failed")
# # #         return jsonify({'error': 'Server error'}), 500

# # # def insert_order(product_id, name, phone, email, quantity, address, notes):
# # #     try:
# # #         db = get_db()
# # #         with db.cursor() as cur:
# # #             cur.execute(
# # #                 "INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) "
# # #                 "VALUES (%s,%s,%s,%s,%s,%s,%s)",
# # #                 (product_id, name, phone, email, quantity, address, notes)
# # #             )
# # #             order_id = cur.lastrowid
# # #         db.commit()
# # #         return order_id
# # #     except OperationalError as e:
# # #         logger.warning("MySQL OperationalError in insert_order: %s", e)
# # #     except Exception:
# # #         logger.exception("DB error in insert_order")
# # #     return None


# # # # @app.route('/order/<int:product_id>', methods=['POST', 'GET'])
# # # # def order(product_id):
# # # #     data = request.get_json() or {}
# # # #     name = (data.get('buyer_name') or '').strip()
# # # #     phone = (data.get('buyer_phone') or '').strip()
# # # #     email = (data.get('buyer_email') or '').strip()
# # # #     try:
# # # #         quantity = int(data.get('quantity') or 1)
# # # #     except Exception:
# # # #         quantity = 1
# # # #     address = data.get('address') or ''
# # # #     notes = data.get('notes') or ''

# # # #     if not name or not phone or quantity < 1:
# # # #         return jsonify({'error': 'Missing required fields'}), 400

# # # #     # ensure product exists
# # # #     p = fetch_product_by_id(product_id)
# # # #     if not p:
# # # #         return jsonify({'error': 'Product not found'}), 404

# # # #     try:
# # # #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# # # #         # optionally: send notification here
# # # #         return jsonify({'success': True, 'order_id': order_id}), 201
# # # #     except Exception as e:
# # # #         # rollback handled in insert_order if exception occurs before commit
# # # #         app.logger.exception("Order insert failed")
# # # #         return jsonify({'error': 'Server error'}), 500

# # # @app.route('/projects')
# # # def projects():
# # #     return render_template('projects.html', title="Projects — Farmyard")

# # # @app.route('/about')
# # # def about():
# # #     return render_template('about.html', title="About — Farmyard")

# # # @app.route('/contact', methods=['GET','POST'])
# # # def contact():
# # #     if request.method == 'POST':
# # #         name = request.form.get('name')
# # #         email = request.form.get('email')
# # #         message = request.form.get('message')
# # #         if not name or not email or not message:
# # #             flash('Please fill in the required fields.', 'danger')
# # #             return redirect(url_for('contact'))
# # #         flash('Message sent — thank you!', 'success')
# # #         return redirect(url_for('contact'))
# # #     return render_template('contact.html', title="Contact — Farmyard")

# # # @app.route('/privacy')
# # # def privacy():
# # #     return render_template('privacy.html', title='Privacy Policy')

# # # @app.route('/terms')
# # # def terms():
# # #     return render_template('terms.html', title='Terms of Service')

# # # # --- Run ---
# # # if __name__ == '__main__':
# # #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))




# # # # # app.py
# # # # import os
# # # # import pymysql
# # # # from pymysql.cursors import DictCursor
# # # # from flask import current_app
# # # # from decimal import Decimal
# # # # from flask import Flask, g, render_template, request, jsonify, abort, url_for, flash, redirect

# # # # # --- Configuration via environment variables ---
# # # # DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
# # # # DB_USER = os.getenv('DB_USER', 'shop_user')
# # # # DB_PASS = os.getenv('DB_PASS', 'secret')
# # # # DB_NAME = os.getenv('DB_NAME', 'shop')
# # # # STATIC_IMAGE_FOLDER = os.getenv('STATIC_IMAGE_FOLDER', 'images')  # static/<folder> where product images live

# # # # app = Flask(__name__)
# # # # app.config['JSON_SORT_KEYS'] = False

# # # # # --- DB helpers ---
# # # # def get_db():
# # # #     if 'db' not in g:
# # # #         g.db = pymysql.connect(
# # # #             host=DB_HOST,
# # # #             user=DB_USER,
# # # #             password=DB_PASS,
# # # #             db=DB_NAME,
# # # #             charset='utf8mb4',
# # # #             cursorclass=DictCursor,
# # # #             autocommit=False
# # # #         )
# # # #     return g.db

# # # # @app.teardown_appcontext
# # # # def close_db(exc):
# # # #     db = g.pop('db', None)
# # # #     if db is not None:
# # # #         try:
# # # #             db.close()
# # # #         except Exception:
# # # #             pass

# # # # def rows_to_json_safe(rows):
# # # #     out = []
# # # #     for r in rows:
# # # #         row = dict(r)
# # # #         # convert Decimal to float for JSON/template formatting
# # # #         if isinstance(row.get('price'), Decimal):
# # # #             row['price'] = float(row['price'])
# # # #         out.append(row)
# # # #     return out


# # # # # --- Data access functions ---
# # # # def fetch_all_products(category=None, q=None):
# # # #     db = get_db()
# # # #     sql = "SELECT id, sku, title, excerpt, description, price, image, category, unit FROM products"
# # # #     params = []
# # # #     where = []
# # # #     if category and category.lower() not in ('all',''):
# # # #         where.append("category = %s")
# # # #         params.append(category)
# # # #     if q:
# # # #         where.append("(title LIKE %s OR excerpt LIKE %s)")
# # # #         like = f"%{q}%"
# # # #         params.extend([like, like])
# # # #     if where:
# # # #         sql += " WHERE " + " AND ".join(where)
# # # #     sql += " ORDER BY created_at DESC"
# # # #     with db.cursor() as cur:
# # # #         cur.execute(sql, params)
# # # #         rows = cur.fetchall()
# # # #     return rows_to_json_safe(rows)

# # # # def fetch_product_by_id(product_id):
# # # #     db = get_db()
# # # #     with db.cursor() as cur:
# # # #         cur.execute("SELECT id, sku, title, excerpt, description, price, image, category, unit FROM products WHERE id=%s", (product_id,))
# # # #         row = cur.fetchone()
# # # #     if row and isinstance(row.get('price'), Decimal):
# # # #         row['price'] = float(row['price'])
# # # #     return row

# # # # def insert_order(product_id, name, phone, email, quantity, address, notes):
# # # #     db = get_db()
# # # #     with db.cursor() as cur:
# # # #         cur.execute("INSERT INTO orders (product_id, buyer_name, buyer_phone, buyer_email, quantity, address, notes) VALUES (%s,%s,%s,%s,%s,%s,%s)",
# # # #                     (product_id, name, phone, email, quantity, address, notes))
# # # #         order_id = cur.lastrowid
# # # #     db.commit()
# # # #     return order_id



# # # # @app.context_processor
# # # # def utility_processor():
# # # #     def endpoint_exists(name):
# # # #         """
# # # #         Return True if an endpoint with the given name is registered.
# # # #         Works for plain view functions and blueprint endpoints (use 'bp.view').
# # # #         """
# # # #         return any(rule.endpoint == name for rule in current_app.url_map.iter_rules())
# # # #     return dict(endpoint_exists=endpoint_exists)


# # # # # --- Routes ---
# # # # @app.route('/')
# # # # def index():
# # # #     return render_template('index.html')

# # # # @app.route('/shop')
# # # # def shop():
# # # #     cat = request.args.get('category')
# # # #     q = request.args.get('q')
# # # #     products = fetch_all_products(category=cat, q=q)
# # # #     # build categories list from DB (distinct)
# # # #     db = get_db()
# # # #     with db.cursor() as cur:
# # # #         cur.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category <> '' ORDER BY category")
# # # #         cats = [r['category'] for r in cur.fetchall()]
# # # #     categories = ['all'] + cats
# # # #     active_category = cat or 'all'
# # # #     return render_template('shop.html',
# # # #                            products=products,
# # # #                            categories=categories,
# # # #                            active_category=active_category,
# # # #                            static_image_folder=STATIC_IMAGE_FOLDER)

# # # # @app.route('/api/product/<int:product_id>')
# # # # def product_api(product_id):
# # # #     p = fetch_product_by_id(product_id)
# # # #     if not p:
# # # #         return jsonify({'error': 'not found'}), 404
# # # #     return jsonify(p)

# # # # @app.route('/product/<int:product_id>')
# # # # def product_detail(product_id):
# # # #     p = fetch_product_by_id(product_id)
# # # #     if not p:
# # # #         abort(404)
# # # #     return render_template('product_detail.html', product=p, static_image_folder=STATIC_IMAGE_FOLDER)

# # # # @app.route('/order/<int:product_id>', methods=['POST'])
# # # # def order(product_id):
# # # #     data = request.get_json() or {}
# # # #     name = (data.get('buyer_name') or '').strip()
# # # #     phone = (data.get('buyer_phone') or '').strip()
# # # #     email = (data.get('buyer_email') or '').strip()
# # # #     try:
# # # #         quantity = int(data.get('quantity') or 1)
# # # #     except Exception:
# # # #         quantity = 1
# # # #     address = data.get('address') or ''
# # # #     notes = data.get('notes') or ''

# # # #     if not name or not phone or quantity < 1:
# # # #         return jsonify({'error': 'Missing required fields'}), 400

# # # #     # ensure product exists
# # # #     p = fetch_product_by_id(product_id)
# # # #     if not p:
# # # #         return jsonify({'error': 'Product not found'}), 404

# # # #     try:
# # # #         order_id = insert_order(product_id, name, phone, email, quantity, address, notes)
# # # #         # optionally: send notification here
# # # #         return jsonify({'success': True, 'order_id': order_id}), 201
# # # #     except Exception as e:
# # # #         # rollback handled in insert_order if exception occurs before commit
# # # #         app.logger.exception("Order insert failed")
# # # #         return jsonify({'error': 'Server error'}), 500


# # # # @app.route('/projects')
# # # # def projects():
# # # #     return render_template('projects.html', title="Projects — Farmyard")

# # # # @app.route('/about')
# # # # def about():
# # # #     return render_template('about.html', title="About — Farmyard")

# # # # @app.route('/contact', methods=['GET','POST'])
# # # # def contact():
# # # #     if request.method == 'POST':
# # # #         name = request.form.get('name')
# # # #         email = request.form.get('email')
# # # #         message = request.form.get('message')
# # # #         if not name or not email or not message:
# # # #             flash('Please fill in the required fields.', 'danger')
# # # #             return redirect(url_for('contact'))
# # # #         flash('Message sent — thank you!', 'success')
# # # #         return redirect(url_for('contact'))
# # # #     return render_template('contact.html', title="Contact — Farmyard")

# # # # @app.route('/privacy')
# # # # def privacy():
# # # #     return render_template('privacy.html', title='Privacy Policy')

# # # # @app.route('/terms')
# # # # def terms():
# # # #     return render_template('terms.html', title='Terms of Service')




# # # # # --- Run ---
# # # # if __name__ == '__main__':
# # # #     app.run(debug=True, host='0.0.0.0', port=int(os.getenv('PORT', 5000)))



# # # # # import os
# # # # # import sqlite3
# # # # # from datetime import datetime
# # # # # from flask import (
# # # # #     Flask, render_template, request, redirect, url_for, flash,
# # # # #     jsonify, g, current_app
# # # # # )
# # # # # from werkzeug.utils import secure_filename

# # # # # # ---------- Configuration ----------
# # # # # BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# # # # # DB_PATH = os.path.join(BASE_DIR, 'data', 'app.db')
# # # # # STATIC_IMAGES = os.path.join(BASE_DIR, 'static', 'images')
# # # # # ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# # # # # app = Flask(__name__)
# # # # # app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
# # # # # app.config['DATABASE'] = DB_PATH
# # # # # app.config['UPLOAD_FOLDER'] = STATIC_IMAGES

# # # # # # Ensure folders exist
# # # # # os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# # # # # os.makedirs(os.path.dirname(app.config['DATABASE']), exist_ok=True)

# # # # # # ---------- Fallback PRODUCTS (used if DB unavailable) ----------
# # # # # PRODUCTS = [
# # # # #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray2.jpeg", "excerpt": "Handpicked premium eggs.", "description": "Premium eggs from free-range hens."},
# # # # #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggs.jpeg", "excerpt": "Fresh, organic eggs.", "description": "Tray of 30 fresh eggs."},
# # # # #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen.", "description": "Kuroiler hens are hardy and productive."},
# # # # #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster.", "description": "Ideal for breeding."},
# # # # #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet.", "description": "Weaned, vaccinated piglet."},
# # # # #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White piglet.", "description": "Ideal for meat production."}
# # # # # ]

# # # # # # ---------- SQLite helpers ----------
# # # # # def get_db():
# # # # #     db = getattr(g, '_database', None)
# # # # #     if db is None:
# # # # #         try:
# # # # #             db = g._database = sqlite3.connect(app.config['DATABASE'])
# # # # #             db.row_factory = sqlite3.Row
# # # # #         except Exception:
# # # # #             db = None
# # # # #     return db

# # # # # def query_db(query, args=(), one=False):
# # # # #     db = get_db()
# # # # #     if not db:
# # # # #         raise RuntimeError("No database available")
# # # # #     cur = db.execute(query, args)
# # # # #     rv = cur.fetchall()
# # # # #     cur.close()
# # # # #     return (rv[0] if rv else None) if one else rv

# # # # # def execute_db(query, args=()):
# # # # #     db = get_db()
# # # # #     if not db:
# # # # #         raise RuntimeError("No database available")
# # # # #     cur = db.execute(query, args)
# # # # #     db.commit()
# # # # #     lastrowid = cur.lastrowid
# # # # #     cur.close()
# # # # #     return lastrowid

# # # # # @app.teardown_appcontext
# # # # # def close_connection(exception):
# # # # #     db = getattr(g, '_database', None)
# # # # #     if db is not None:
# # # # #         db.close()

# # # # # # ---------- Utilities ----------
# # # # # def allowed_file(filename):
# # # # #     return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# # # # # def save_uploaded_image(file_storage):
# # # # #     if not file_storage or file_storage.filename == '':
# # # # #         return None
# # # # #     filename = secure_filename(file_storage.filename)
# # # # #     if not allowed_file(filename):
# # # # #         return None
# # # # #     dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
# # # # #     file_storage.save(dest)
# # # # #     return filename

# # # # # # ---------- DB init & seed (optional) ----------
# # # # # def init_db_and_seed():
# # # # #     conn = sqlite3.connect(app.config['DATABASE'])
# # # # #     c = conn.cursor()
# # # # #     c.executescript("""
# # # # #     CREATE TABLE IF NOT EXISTS products (
# # # # #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# # # # #       category TEXT NOT NULL,
# # # # #       title TEXT NOT NULL,
# # # # #       excerpt TEXT,
# # # # #       description TEXT,
# # # # #       price INTEGER NOT NULL,
# # # # #       unit TEXT,
# # # # #       image TEXT
# # # # #     );
# # # # #     """)
# # # # #     c.execute("SELECT COUNT(1) FROM products")
# # # # #     if c.fetchone()[0] == 0:
# # # # #         seed = [
# # # # #             ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs.','Premium eggs from free-range hens',150,'per dozen','eggtray.jpeg'),
# # # # #             ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs.','Tray of 30 fresh eggs',350,'per tray (30 eggs)','eggs.jpeg'),
# # # # #             ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Kuroiler hens are hardy and productive.',1100,'per bird','hen.jpeg'),
# # # # #             ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Ideal for smallholder breeding programs.',1500,'per bird','cock.jpeg'),
# # # # #             ('pigs','Landrace Piglet','Healthy Landrace piglet.','Weaned Landrace piglet, vaccinated and dewormed.',8000,'per piglet','piglet.jpeg'),
# # # # #             ('pigs','Large White Piglet','Large White breed piglet.','Large White piglet, ideal for meat production.',9000,'per piglet','piglet1.jpeg'),
# # # # #         ]
# # # # #         c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
# # # # #         conn.commit()
# # # # #     conn.close()

# # # # # if not os.path.exists(app.config['DATABASE']):
# # # # #     init_db_and_seed()

# # # # # # ---------- Context processors ----------
# # # # # @app.context_processor
# # # # # def inject_now():
# # # # #     return {'current_year': datetime.utcnow().year}

# # # # # @app.context_processor
# # # # # def utility_processor():
# # # # #     def endpoint_exists(name):
# # # # #         return name in current_app.view_functions
# # # # #     return dict(endpoint_exists=endpoint_exists)

# # # # # # ---------- Routes ----------
# # # # # @app.route('/')
# # # # # def index():
# # # # #     try:
# # # # #         rows = query_db("SELECT * FROM products ORDER BY id DESC LIMIT 4")
# # # # #         products = [dict(r) for r in rows] if rows else []
# # # # #     except Exception:
# # # # #         products = PRODUCTS[:4]
# # # # #     return render_template('index.html', title="Farmyard — Kisumu's Premier Farm", products=products)

# # # # # @app.route('/shop')
# # # # # def shop():
# # # # #     category = request.args.get('category', 'all')
# # # # #     q = request.args.get('q', '').strip().lower()
# # # # #     try:
# # # # #         sql = "SELECT id, category, title, price, unit, image, excerpt FROM products"
# # # # #         params = []
# # # # #         where = []
# # # # #         if category != 'all':
# # # # #             where.append("category = ?")
# # # # #             params.append(category)
# # # # #         if q:
# # # # #             where.append("LOWER(title) LIKE ?")
# # # # #             params.append(f"%{q}%")
# # # # #         if where:
# # # # #             sql += " WHERE " + " AND ".join(where)
# # # # #         sql += " ORDER BY id DESC"
# # # # #         rows = query_db(sql, params)
# # # # #         products = [dict(r) for r in rows]
# # # # #     except Exception:
# # # # #         products = PRODUCTS.copy()
# # # # #         if category != 'all':
# # # # #             products = [p for p in products if p['category'] == category]
# # # # #         if q:
# # # # #             products = [p for p in products if q in p['title'].lower()]
# # # # #     categories = ['all', 'eggs', 'poultry', 'pigs']
# # # # #     return render_template('shop.html', products=products, categories=categories, active_category=category)

# # # # # @app.route('/api/product/<int:product_id>')
# # # # # def product_api(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if row:
# # # # #             return jsonify(dict(row))
# # # # #     except Exception:
# # # # #         pass
# # # # #     p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #     if not p:
# # # # #         return jsonify({"error": "Not found"}), 404
# # # # #     return jsonify(p)

# # # # # @app.route('/product/<int:product_id>')
# # # # # def product_detail(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if row:
# # # # #             product = dict(row)
# # # # #         else:
# # # # #             raise LookupError
# # # # #     except Exception:
# # # # #         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         if not product:
# # # # #             flash('Product not found.', 'danger')
# # # # #             return redirect(url_for('shop'))
# # # # #     return render_template('product_detail.html', product=product)

# # # # # @app.route('/order/<int:product_id>', methods=['GET','POST'])
# # # # # def order(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT title FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         title = row['title'] if row else None
# # # # #     except Exception:
# # # # #         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         title = p['title'] if p else None
# # # # #     if not title:
# # # # #         flash('Product not found', 'danger')
# # # # #         return redirect(url_for('shop'))
# # # # #     flash(f'Order request received for {title}. We will contact you shortly.', 'success')
# # # # #     return redirect(url_for('product_detail', product_id=product_id))

# # # # # # Admin create/edit/delete (optional)
# # # # # @app.route('/admin/products/new', methods=['GET','POST'])
# # # # # def product_create():
# # # # #     if request.method == 'POST':
# # # # #         title = request.form.get('title','').strip()
# # # # #         category = request.form.get('category','eggs')
# # # # #         price = int(request.form.get('price') or 0)
# # # # #         unit = request.form.get('unit','per item')
# # # # #         excerpt = request.form.get('excerpt','')
# # # # #         description = request.form.get('description','')
# # # # #         image_file = request.files.get('image')
# # # # #         filename = save_uploaded_image(image_file) if image_file else None
# # # # #         try:
# # # # #             execute_db(
# # # # #                 "INSERT INTO products (category, title, excerpt, description, price, unit, image) VALUES (?,?,?,?,?,?,?)",
# # # # #                 (category, title, excerpt, description, price, unit, filename)
# # # # #             )
# # # # #             flash('Product created.', 'success')
# # # # #             return redirect(url_for('shop'))
# # # # #         except Exception:
# # # # #             new_id = max(p['id'] for p in PRODUCTS) + 1 if PRODUCTS else 1
# # # # #             PRODUCTS.append({
# # # # #                 "id": new_id, "category": category, "title": title, "price": price,
# # # # #                 "unit": unit, "image": filename or 'placeholder.jpg', "excerpt": excerpt, "description": description
# # # # #             })
# # # # #             flash('Product created (in-memory fallback).', 'success')
# # # # #             return redirect(url_for('shop'))
# # # # #     return render_template('product_form.html', action='Create', product=None)

# # # # # @app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
# # # # # def product_edit(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if not row:
# # # # #             raise LookupError
# # # # #         product = dict(row)
# # # # #     except Exception:
# # # # #         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         if not product:
# # # # #             flash('Not found', 'danger')
# # # # #             return redirect(url_for('shop'))
# # # # #     if request.method == 'POST':
# # # # #         title = request.form.get('title', product.get('title')).strip()
# # # # #         category = request.form.get('category', product.get('category'))
# # # # #         price = int(request.form.get('price') or product.get('price', 0))
# # # # #         unit = request.form.get('unit', product.get('unit'))
# # # # #         excerpt = request.form.get('excerpt', product.get('excerpt'))
# # # # #         description = request.form.get('description', product.get('description'))
# # # # #         image_file = request.files.get('image')
# # # # #         filename = product.get('image')
# # # # #         if image_file and image_file.filename:
# # # # #             saved = save_uploaded_image(image_file)
# # # # #             if saved:
# # # # #                 filename = saved
# # # # #         try:
# # # # #             execute_db(
# # # # #                 "UPDATE products SET category=?, title=?, excerpt=?, description=?, price=?, unit=?, image=? WHERE id=?",
# # # # #                 (category, title, excerpt, description, price, unit, filename, product_id)
# # # # #             )
# # # # #             flash('Product updated.', 'success')
# # # # #             return redirect(url_for('product_detail', product_id=product_id))
# # # # #         except Exception:
# # # # #             for p in PRODUCTS:
# # # # #                 if p['id'] == product_id:
# # # # #                     p.update({"category": category, "title": title, "excerpt": excerpt, "description": description, "price": price, "unit": unit, "image": filename})
# # # # #                     break
# # # # #             flash('Product updated (in-memory fallback).', 'success')
# # # # #             return redirect(url_for('product_detail', product_id=product_id))
# # # # #     return render_template('product_form.html', action='Edit', product=product)

# # # # # @app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
# # # # # def product_delete(product_id):
# # # # #     try:
# # # # #         execute_db("DELETE FROM products WHERE id = ?", (product_id,))
# # # # #         flash('Product deleted.', 'success')
# # # # #     except Exception:
# # # # #         global PRODUCTS
# # # # #         PRODUCTS = [p for p in PRODUCTS if p['id'] != product_id]
# # # # #         flash('Product deleted (in-memory fallback).', 'success')
# # # # #     return redirect(url_for('shop'))

# # # # # # Simple pages
# # # # # @app.route('/projects')
# # # # # def projects():
# # # # #     return render_template('projects.html', title="Projects — Farmyard")

# # # # # @app.route('/about')
# # # # # def about():
# # # # #     return render_template('about.html', title="About — Farmyard")

# # # # # @app.route('/contact', methods=['GET','POST'])
# # # # # def contact():
# # # # #     if request.method == 'POST':
# # # # #         name = request.form.get('name')
# # # # #         email = request.form.get('email')
# # # # #         message = request.form.get('message')
# # # # #         if not name or not email or not message:
# # # # #             flash('Please fill in the required fields.', 'danger')
# # # # #             return redirect(url_for('contact'))
# # # # #         flash('Message sent — thank you!', 'success')
# # # # #         return redirect(url_for('contact'))
# # # # #     return render_template('contact.html', title="Contact — Farmyard")

# # # # # @app.route('/privacy')
# # # # # def privacy():
# # # # #     return render_template('privacy.html', title='Privacy Policy')

# # # # # @app.route('/terms')
# # # # # def terms():
# # # # #     return render_template('terms.html', title='Terms of Service')

# # # # # # ---------- Run ----------
# # # # # if __name__ == '__main__':
# # # # #     app.run(debug=True, host='0.0.0.0', port=5000)




# # # # # import os
# # # # # import sqlite3
# # # # # from datetime import datetime
# # # # # from flask import (
# # # # #     Flask, render_template, request, redirect, url_for, flash,
# # # # #     jsonify, g, current_app
# # # # # )
# # # # # from werkzeug.utils import secure_filename

# # # # # # ---------- Configuration ----------
# # # # # BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# # # # # DB_PATH = os.path.join(BASE_DIR, 'data', 'app.db')
# # # # # STATIC_IMAGES = os.path.join(BASE_DIR, 'static', 'images')
# # # # # ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# # # # # app = Flask(__name__)
# # # # # app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
# # # # # app.config['DATABASE'] = DB_PATH
# # # # # app.config['UPLOAD_FOLDER'] = STATIC_IMAGES

# # # # # # Ensure folders exist
# # # # # os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
# # # # # os.makedirs(os.path.dirname(app.config['DATABASE']), exist_ok=True)

# # # # # # ---------- Sample fallback PRODUCTS (used if DB unavailable) ----------
# # # # # PRODUCTS = [
# # # # #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray.jpeg", "excerpt": "Handpicked premium eggs, perfect for baking or daily use.", "description": "Premium eggs from free-range hens."},
# # # # #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggtry.jpeg", "excerpt": "Fresh, organic eggs from free-range hens.", "description": "Tray of 30 fresh eggs."},
# # # # #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen with superior egg production.", "description": "Kuroiler hens are hardy and productive."},
# # # # #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster, excellent for breeding.", "description": "Ideal for smallholder breeding programs."},
# # # # #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet, weaned and ready for rearing.", "description": "Weaned Landrace piglet, vaccinated and dewormed."},
# # # # #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White breed piglet known for rapid growth.", "description": "Large White piglet, ideal for meat production."}
# # # # # ]

# # # # # # ---------- SQLite helpers (optional) ----------
# # # # # def get_db():
# # # # #     db = getattr(g, '_database', None)
# # # # #     if db is None:
# # # # #         try:
# # # # #             db = g._database = sqlite3.connect(app.config['DATABASE'])
# # # # #             db.row_factory = sqlite3.Row
# # # # #         except Exception:
# # # # #             db = None
# # # # #     return db

# # # # # def query_db(query, args=(), one=False):
# # # # #     db = get_db()
# # # # #     if not db:
# # # # #         raise RuntimeError("No database available")
# # # # #     cur = db.execute(query, args)
# # # # #     rv = cur.fetchall()
# # # # #     cur.close()
# # # # #     return (rv[0] if rv else None) if one else rv

# # # # # def execute_db(query, args=()):
# # # # #     db = get_db()
# # # # #     if not db:
# # # # #         raise RuntimeError("No database available")
# # # # #     cur = db.execute(query, args)
# # # # #     db.commit()
# # # # #     lastrowid = cur.lastrowid
# # # # #     cur.close()
# # # # #     return lastrowid

# # # # # @app.teardown_appcontext
# # # # # def close_connection(exception):
# # # # #     db = getattr(g, '_database', None)
# # # # #     if db is not None:
# # # # #         db.close()

# # # # # # ---------- Utility ----------
# # # # # def allowed_file(filename):
# # # # #     return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# # # # # def save_uploaded_image(file_storage):
# # # # #     if not file_storage or file_storage.filename == '':
# # # # #         return None
# # # # #     filename = secure_filename(file_storage.filename)
# # # # #     if not allowed_file(filename):
# # # # #         return None
# # # # #     dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
# # # # #     file_storage.save(dest)
# # # # #     return filename

# # # # # # ---------- DB initialization & seed (optional) ----------
# # # # # def init_db_and_seed():
# # # # #     conn = sqlite3.connect(app.config['DATABASE'])
# # # # #     c = conn.cursor()
# # # # #     c.executescript("""
# # # # #     CREATE TABLE IF NOT EXISTS products (
# # # # #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# # # # #       category TEXT NOT NULL,
# # # # #       title TEXT NOT NULL,
# # # # #       excerpt TEXT,
# # # # #       description TEXT,
# # # # #       price INTEGER NOT NULL,
# # # # #       unit TEXT,
# # # # #       image TEXT
# # # # #     );
# # # # #     """)
# # # # #     c.execute("SELECT COUNT(1) FROM products")
# # # # #     if c.fetchone()[0] == 0:
# # # # #         seed = [
# # # # #             ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs, perfect for baking.','Premium eggs from free-range hens',150,'per dozen','eggtray.jpeg'),
# # # # #             ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs from free-range hens.','Tray of 30 fresh eggs',350,'per tray (30 eggs)','eggtry.jpeg'),
# # # # #             ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Kuroiler hens are hardy and productive.',1100,'per bird','hen.jpeg'),
# # # # #             ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Ideal for smallholder breeding programs.',1500,'per bird','cock.jpeg'),
# # # # #             ('pigs','Landrace Piglet','Healthy Landrace piglet, weaned and ready.','Weaned Landrace piglet, vaccinated and dewormed.',8000,'per piglet','piglet.jpeg'),
# # # # #             ('pigs','Large White Piglet','Large White breed piglet known for rapid growth.','Large White piglet, ideal for meat production.',9000,'per piglet','piglet1.jpeg'),
# # # # #         ]
# # # # #         c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
# # # # #         conn.commit()
# # # # #     conn.close()

# # # # # # Create DB and seed if missing
# # # # # if not os.path.exists(app.config['DATABASE']):
# # # # #     init_db_and_seed()

# # # # # # ---------- Context processors ----------
# # # # # @app.context_processor
# # # # # def inject_now():
# # # # #     return {'current_year': datetime.utcnow().year}

# # # # # @app.context_processor
# # # # # def utility_processor():
# # # # #     def endpoint_exists(name):
# # # # #         return name in current_app.view_functions
# # # # #     return dict(endpoint_exists=endpoint_exists)

# # # # # # ---------- Routes ----------
# # # # # @app.route('/')
# # # # # def index():
# # # # #     try:
# # # # #         rows = query_db("SELECT * FROM products ORDER BY id DESC LIMIT 4")
# # # # #         products = [dict(r) for r in rows] if rows else []
# # # # #     except Exception:
# # # # #         products = PRODUCTS[:4]
# # # # #     return render_template('index.html', title="Farmyard — Kisumu's Premier Farm", products=products)

# # # # # @app.route('/shop')
# # # # # def shop():
# # # # #     category = request.args.get('category', 'all')
# # # # #     q = request.args.get('q', '').strip().lower()
# # # # #     try:
# # # # #         sql = "SELECT id, category, title, price, unit, image, excerpt FROM products"
# # # # #         params = []
# # # # #         where = []
# # # # #         if category != 'all':
# # # # #             where.append("category = ?")
# # # # #             params.append(category)
# # # # #         if q:
# # # # #             where.append("LOWER(title) LIKE ?")
# # # # #             params.append(f"%{q}%")
# # # # #         if where:
# # # # #             sql += " WHERE " + " AND ".join(where)
# # # # #         sql += " ORDER BY id DESC"
# # # # #         rows = query_db(sql, params)
# # # # #         products = [dict(r) for r in rows]
# # # # #     except Exception:
# # # # #         products = PRODUCTS.copy()
# # # # #         if category != 'all':
# # # # #             products = [p for p in products if p['category'] == category]
# # # # #         if q:
# # # # #             products = [p for p in products if q in p['title'].lower()]
# # # # #     categories = ['all', 'eggs', 'poultry', 'pigs']
# # # # #     return render_template('shop.html', products=products, categories=categories, active_category=category)

# # # # # @app.route('/api/product/<int:product_id>')
# # # # # def product_api(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if row:
# # # # #             return jsonify(dict(row))
# # # # #     except Exception:
# # # # #         pass
# # # # #     p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #     if not p:
# # # # #         return jsonify({"error": "Not found"}), 404
# # # # #     return jsonify(p)

# # # # # @app.route('/product/<int:product_id>')
# # # # # def product_detail(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if row:
# # # # #             product = dict(row)
# # # # #         else:
# # # # #             raise LookupError
# # # # #     except Exception:
# # # # #         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         if not product:
# # # # #             flash('Product not found.', 'danger')
# # # # #             return redirect(url_for('shop'))
# # # # #     return render_template('product_detail.html', product=product)

# # # # # @app.route('/order/<int:product_id>', methods=['GET','POST'])
# # # # # def order(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT title FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         title = row['title'] if row else None
# # # # #     except Exception:
# # # # #         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         title = p['title'] if p else None
# # # # #     if not title:
# # # # #         flash('Product not found', 'danger')
# # # # #         return redirect(url_for('shop'))
# # # # #     flash(f'Order request received for {title}. We will contact you shortly.', 'success')
# # # # #     return redirect(url_for('product_detail', product_id=product_id))

# # # # # # Admin create/edit/delete (optional)
# # # # # @app.route('/admin/products/new', methods=['GET','POST'])
# # # # # def product_create():
# # # # #     if request.method == 'POST':
# # # # #         title = request.form.get('title','').strip()
# # # # #         category = request.form.get('category','eggs')
# # # # #         price = int(request.form.get('price') or 0)
# # # # #         unit = request.form.get('unit','per item')
# # # # #         excerpt = request.form.get('excerpt','')
# # # # #         description = request.form.get('description','')
# # # # #         image_file = request.files.get('image')
# # # # #         filename = save_uploaded_image(image_file) if image_file else None
# # # # #         try:
# # # # #             execute_db(
# # # # #                 "INSERT INTO products (category, title, excerpt, description, price, unit, image) VALUES (?,?,?,?,?,?,?)",
# # # # #                 (category, title, excerpt, description, price, unit, filename)
# # # # #             )
# # # # #             flash('Product created.', 'success')
# # # # #             return redirect(url_for('shop'))
# # # # #         except Exception:
# # # # #             new_id = max(p['id'] for p in PRODUCTS) + 1 if PRODUCTS else 1
# # # # #             PRODUCTS.append({
# # # # #                 "id": new_id, "category": category, "title": title, "price": price,
# # # # #                 "unit": unit, "image": filename or 'placeholder.jpg', "excerpt": excerpt, "description": description
# # # # #             })
# # # # #             flash('Product created (in-memory fallback).', 'success')
# # # # #             return redirect(url_for('shop'))
# # # # #     return render_template('product_form.html', action='Create', product=None)

# # # # # @app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
# # # # # def product_edit(product_id):
# # # # #     try:
# # # # #         row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # #         if not row:
# # # # #             raise LookupError
# # # # #         product = dict(row)
# # # # #     except Exception:
# # # # #         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # #         if not product:
# # # # #             flash('Not found', 'danger')
# # # # #             return redirect(url_for('shop'))
# # # # #     if request.method == 'POST':
# # # # #         title = request.form.get('title', product.get('title')).strip()
# # # # #         category = request.form.get('category', product.get('category'))
# # # # #         price = int(request.form.get('price') or product.get('price', 0))
# # # # #         unit = request.form.get('unit', product.get('unit'))
# # # # #         excerpt = request.form.get('excerpt', product.get('excerpt'))
# # # # #         description = request.form.get('description', product.get('description'))
# # # # #         image_file = request.files.get('image')
# # # # #         filename = product.get('image')
# # # # #         if image_file and image_file.filename:
# # # # #             saved = save_uploaded_image(image_file)
# # # # #             if saved:
# # # # #                 filename = saved
# # # # #         try:
# # # # #             execute_db(
# # # # #                 "UPDATE products SET category=?, title=?, excerpt=?, description=?, price=?, unit=?, image=? WHERE id=?",
# # # # #                 (category, title, excerpt, description, price, unit, filename, product_id)
# # # # #             )
# # # # #             flash('Product updated.', 'success')
# # # # #             return redirect(url_for('product_detail', product_id=product_id))
# # # # #         except Exception:
# # # # #             for p in PRODUCTS:
# # # # #                 if p['id'] == product_id:
# # # # #                     p.update({"category": category, "title": title, "excerpt": excerpt, "description": description, "price": price, "unit": unit, "image": filename})
# # # # #                     break
# # # # #             flash('Product updated (in-memory fallback).', 'success')
# # # # #             return redirect(url_for('product_detail', product_id=product_id))
# # # # #     return render_template('product_form.html', action='Edit', product=product)

# # # # # @app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
# # # # # def product_delete(product_id):
# # # # #     try:
# # # # #         execute_db("DELETE FROM products WHERE id = ?", (product_id,))
# # # # #         flash('Product deleted.', 'success')
# # # # #     except Exception:
# # # # #         global PRODUCTS
# # # # #         PRODUCTS = [p for p in PRODUCTS if p['id'] != product_id]
# # # # #         flash('Product deleted (in-memory fallback).', 'success')
# # # # #     return redirect(url_for('shop'))

# # # # # # Simple pages
# # # # # @app.route('/projects')
# # # # # def projects():
# # # # #     return render_template('projects.html', title="Projects — Farmyard")

# # # # # @app.route('/about')
# # # # # def about():
# # # # #     return render_template('about.html', title="About — Farmyard")

# # # # # @app.route('/contact', methods=['GET','POST'])
# # # # # def contact():
# # # # #     if request.method == 'POST':
# # # # #         name = request.form.get('name')
# # # # #         email = request.form.get('email')
# # # # #         message = request.form.get('message')
# # # # #         if not name or not email or not message:
# # # # #             flash('Please fill in the required fields.', 'danger')
# # # # #             return redirect(url_for('contact'))
# # # # #         flash('Message sent — thank you!', 'success')
# # # # #         return redirect(url_for('contact'))
# # # # #     return render_template('contact.html', title="Contact — Farmyard")

# # # # # @app.route('/privacy')
# # # # # def privacy():
# # # # #     return render_template('privacy.html', title='Privacy Policy')

# # # # # @app.route('/terms')
# # # # # def terms():
# # # # #     return render_template('terms.html', title='Terms of Service')

# # # # # # ---------- Run ----------
# # # # # if __name__ == '__main__':
# # # # #     app.run(debug=True, host='0.0.0.0', port=5000)





# # # # # # import os
# # # # # # import sqlite3
# # # # # # from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g
# # # # # # from werkzeug.utils import secure_filename
# # # # # # from datetime import datetime

# # # # # # BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# # # # # # DB_PATH = os.path.join(BASE_DIR, 'farmyard.db')
# # # # # # UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'images')
# # # # # # os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# # # # # # app = Flask(__name__)
# # # # # # app.config['SECRET_KEY'] = 'replace-with-a-secure-secret'
# # # # # # app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# # # # # # app.config['DATABASE'] = DB_PATH

# # # # # # # -------------------------
# # # # # # # SQLite helpers (no ORM)
# # # # # # # -------------------------
# # # # # # def get_db():
# # # # # #     if 'db' not in g:
# # # # # #         g.db = sqlite3.connect(app.config['DATABASE'])
# # # # # #         g.db.row_factory = sqlite3.Row
# # # # # #     return g.db

# # # # # # @app.teardown_appcontext
# # # # # # def close_db(exc):
# # # # # #     db = g.pop('db', None)
# # # # # #     if db is not None:
# # # # # #         db.close()

# # # # # # def query_db(query, args=(), one=False):
# # # # # #     cur = get_db().execute(query, args)
# # # # # #     rv = cur.fetchall()
# # # # # #     cur.close()
# # # # # #     return (rv[0] if rv else None) if one else rv

# # # # # # def execute_db(query, args=()):
# # # # # #     db = get_db()
# # # # # #     cur = db.execute(query, args)
# # # # # #     db.commit()
# # # # # #     lastrowid = cur.lastrowid
# # # # # #     cur.close()
# # # # # #     return lastrowid

# # # # # # # -------------------------
# # # # # # # DB initialization & seed
# # # # # # # -------------------------
# # # # # # def init_db_and_seed():
# # # # # #     """Create products table and seed sample products if empty."""
# # # # # #     conn = sqlite3.connect(DB_PATH)
# # # # # #     c = conn.cursor()
# # # # # #     c.executescript("""
# # # # # #     CREATE TABLE IF NOT EXISTS products (
# # # # # #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# # # # # #       category TEXT NOT NULL,
# # # # # #       title TEXT NOT NULL,
# # # # # #       excerpt TEXT,
# # # # # #       description TEXT,
# # # # # #       price INTEGER NOT NULL,
# # # # # #       unit TEXT,
# # # # # #       image TEXT
# # # # # #     );
# # # # # #     """)
# # # # # #     c.execute("SELECT COUNT(1) FROM products")
# # # # # #     if c.fetchone()[0] == 0:
# # # # # #         seed = [
# # # # # #             ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs, perfect for baking.','Full description for premium dozen eggs',150,'per dozen','eggs-dozen.jpg'),
# # # # # #             ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs from free-range hens.','Full description for tray',350,'per tray (30 eggs)','eggs-tray.jpg'),
# # # # # #             ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Full description for Kuroiler',1100,'per bird','kuroiler.jpg'),
# # # # # #             ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Full description for rooster',1500,'per bird','rhode-island-red.jpg'),
# # # # # #             ('pigs','Landrace Piglet','Healthy Landrace piglet, weaned and ready.','Full description for landrace',8000,'per piglet','landrace-piglet.jpg'),
# # # # # #             ('pigs','Large White Piglet','Large White breed piglet known for rapid growth.','Full description for large white',9000,'per piglet','large-white-piglet.jpg'),
# # # # # #         ]
# # # # # #         c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
# # # # # #         conn.commit()
# # # # # #     conn.close()

# # # # # # # Ensure DB exists and seeded on startup
# # # # # # if not os.path.exists(DB_PATH):
# # # # # #     init_db_and_seed()

# # # # # # # -------------------------
# # # # # # # Sample fallback PRODUCTS (used only if DB access fails)
# # # # # # # -------------------------
# # # # # # PRODUCTS = [
# # # # # #     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray.jpeg", "excerpt": "Handpicked premium eggs, perfect for baking or daily use."},
# # # # # #     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggtry.jpeg", "excerpt": "Fresh, organic eggs from free-range hens."},
# # # # # #     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen with superior egg production."},
# # # # # #     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster, excellent for breeding."},
# # # # # #     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet, weaned and ready for rearing."},
# # # # # #     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White breed piglet known for rapid growth."}
# # # # # # ]

# # # # # # # -------------------------
# # # # # # # Context processor
# # # # # # # -------------------------
# # # # # # @app.context_processor
# # # # # # def inject_now():
# # # # # #     return {'current_year': datetime.utcnow().year}

# # # # # # # -------------------------
# # # # # # # Routes
# # # # # # # -------------------------
# # # # # # @app.route('/')
# # # # # # def index():
# # # # # #     """
# # # # # #     Render homepage with a safe 'products' variable for the template.
# # # # # #     Tries SQLite first, falls back to the in-memory PRODUCTS list.
# # # # # #     """
# # # # # #     products = []
# # # # # #     try:
# # # # # #         rows = query_db("SELECT * FROM products ORDER BY id DESC LIMIT 4")
# # # # # #         products = [dict(r) for r in rows] if rows else []
# # # # # #     except Exception:
# # # # # #         try:
# # # # # #             products = PRODUCTS[:4]
# # # # # #         except NameError:
# # # # # #             products = []
# # # # # #     return render_template('index.html', title="Farmyard — Kisumu's Premier Farm", products=products)

# # # # # # # app.py (add once near other imports / context processors)
# # # # # # from flask import current_app

# # # # # # @app.context_processor
# # # # # # def utility_processor():
# # # # # #     def endpoint_exists(name):
# # # # # #         return name in current_app.view_functions
# # # # # #     return dict(endpoint_exists=endpoint_exists)


# # # # # # @app.route('/shop')
# # # # # # def shop():
# # # # # #     category = request.args.get('category', 'all')
# # # # # #     q = request.args.get('q', '').strip()
# # # # # #     sql = "SELECT * FROM products"
# # # # # #     params = []
# # # # # #     where = []
# # # # # #     if category != 'all':
# # # # # #         where.append("category = ?")
# # # # # #         params.append(category)
# # # # # #     if q:
# # # # # #         where.append("title LIKE ?")
# # # # # #         params.append(f"%{q}%")
# # # # # #     if where:
# # # # # #         sql += " WHERE " + " AND ".join(where)
# # # # # #     sql += " ORDER BY id DESC"
# # # # # #     try:
# # # # # #         products = query_db(sql, params)
# # # # # #         products = [dict(p) for p in products]
# # # # # #     except Exception:
# # # # # #         products = PRODUCTS if category == 'all' else [p for p in PRODUCTS if p['category'] == category]
# # # # # #     categories = ['all', 'eggs', 'poultry', 'pigs']
# # # # # #     return render_template('shop.html', products=products, categories=categories, active_category=category)

# # # # # # # API for modal
# # # # # # @app.route('/api/product/<int:product_id>')
# # # # # # def product_api(product_id):
# # # # # #     try:
# # # # # #         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # # #         if not p:
# # # # # #             return jsonify({"error":"not found"}), 404
# # # # # #         return jsonify(dict(p))
# # # # # #     except Exception:
# # # # # #         # fallback to sample list
# # # # # #         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # # #         if not p:
# # # # # #             return jsonify({"error":"not found"}), 404
# # # # # #         return jsonify(p)

# # # # # # @app.route('/product/<int:product_id>')
# # # # # # def product_detail(product_id):
# # # # # #     try:
# # # # # #         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # # #         if not p:
# # # # # #             flash('Product not found.', 'danger')
# # # # # #             return redirect(url_for('shop'))
# # # # # #         product = dict(p)
# # # # # #     except Exception:
# # # # # #         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # # #         if not product:
# # # # # #             flash('Product not found.', 'danger')
# # # # # #             return redirect(url_for('shop'))
# # # # # #     return render_template('product_detail.html', product=product)

# # # # # # # Create product (admin)
# # # # # # @app.route('/admin/products/new', methods=['GET','POST'])
# # # # # # def product_create():
# # # # # #     if request.method == 'POST':
# # # # # #         title = request.form.get('title','').strip()
# # # # # #         category = request.form.get('category','eggs')
# # # # # #         price = int(request.form.get('price') or 0)
# # # # # #         unit = request.form.get('unit','per item')
# # # # # #         excerpt = request.form.get('excerpt','')
# # # # # #         description = request.form.get('description','')
# # # # # #         image_file = request.files.get('image')
# # # # # #         filename = None
# # # # # #         if image_file and image_file.filename:
# # # # # #             filename = secure_filename(image_file.filename)
# # # # # #             image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
# # # # # #         execute_db(
# # # # # #             "INSERT INTO products (category, title, excerpt, description, price, unit, image) VALUES (?,?,?,?,?,?,?)",
# # # # # #             (category, title, excerpt, description, price, unit, filename)
# # # # # #         )
# # # # # #         flash('Product created.', 'success')
# # # # # #         return redirect(url_for('shop'))
# # # # # #     return render_template('product_form.html', action='Create', product=None)

# # # # # # # Edit product (admin)
# # # # # # @app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
# # # # # # def product_edit(product_id):
# # # # # #     p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # # #     if not p:
# # # # # #         flash('Not found', 'danger'); return redirect(url_for('shop'))
# # # # # #     if request.method == 'POST':
# # # # # #         title = request.form.get('title', p['title']).strip()
# # # # # #         category = request.form.get('category', p['category'])
# # # # # #         price = int(request.form.get('price') or p['price'])
# # # # # #         unit = request.form.get('unit', p['unit'])
# # # # # #         excerpt = request.form.get('excerpt', p['excerpt'])
# # # # # #         description = request.form.get('description', p['description'])
# # # # # #         image_file = request.files.get('image')
# # # # # #         filename = p['image']
# # # # # #         if image_file and image_file.filename:
# # # # # #             filename = secure_filename(image_file.filename)
# # # # # #             image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
# # # # # #         execute_db(
# # # # # #             "UPDATE products SET category=?, title=?, excerpt=?, description=?, price=?, unit=?, image=? WHERE id=?",
# # # # # #             (category, title, excerpt, description, price, unit, filename, product_id)
# # # # # #         )
# # # # # #         flash('Product updated.', 'success')
# # # # # #         return redirect(url_for('product_detail', product_id=product_id))
# # # # # #     return render_template('product_form.html', action='Edit', product=dict(p))

# # # # # # # Delete product (admin)
# # # # # # @app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
# # # # # # def product_delete(product_id):
# # # # # #     execute_db("DELETE FROM products WHERE id = ?", (product_id,))
# # # # # #     flash('Product deleted.', 'success')
# # # # # #     return redirect(url_for('shop'))

# # # # # # # Contact (GET/POST)
# # # # # # @app.route('/contact', methods=['GET','POST'])
# # # # # # def contact():
# # # # # #     if request.method == 'POST':
# # # # # #         name = request.form.get('name')
# # # # # #         phone = request.form.get('phone')
# # # # # #         email = request.form.get('email')
# # # # # #         message = request.form.get('message')
# # # # # #         if not name or not email or not message:
# # # # # #             flash('Please fill in the required fields.', 'danger')
# # # # # #             return redirect(url_for('contact'))
# # # # # #         flash('Message sent — thank you!', 'success')
# # # # # #         return redirect(url_for('contact'))
# # # # # #     return render_template('contact.html', title="Contact — Farmyard")

# # # # # # # Simple order placeholder
# # # # # # @app.route('/order/<int:product_id>', methods=['GET','POST'])
# # # # # # def order(product_id):
# # # # # #     try:
# # # # # #         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
# # # # # #         title = p['title'] if p else None
# # # # # #     except Exception:
# # # # # #         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
# # # # # #         title = p['title'] if p else None
# # # # # #     if not title:
# # # # # #         flash('Product not found', 'danger')
# # # # # #         return redirect(url_for('shop'))
# # # # # #     flash(f'Order request received for {title}. We will contact you.', 'success')
# # # # # #     return redirect(url_for('product_detail', product_id=product_id))

# # # # # # # Simple pages
# # # # # # @app.route('/projects')
# # # # # # def projects():
# # # # # #     return render_template('projects.html', title="Projects — Farmyard")

# # # # # # @app.route('/about')
# # # # # # def about():
# # # # # #     return render_template('about.html', title="About — Farmyard")


# # # # # # # app.py

# # # # # # @app.route('/privacy')
# # # # # # def privacy():
# # # # # #     return render_template('privacy.html', title='Privacy Policy')

# # # # # # @app.route('/terms')
# # # # # # def terms():
# # # # # #     return render_template('terms.html', title='Terms of Service')



# # # # # # if __name__ == '__main__':
# # # # # #     app.run(debug=True, host='0.0.0.0', port=5000)
