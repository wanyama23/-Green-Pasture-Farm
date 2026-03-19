import os
import sqlite3
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    jsonify, g, current_app
)
from werkzeug.utils import secure_filename

# ---------- Configuration ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'app.db')
STATIC_IMAGES = os.path.join(BASE_DIR, 'static', 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
app.config['DATABASE'] = DB_PATH
app.config['UPLOAD_FOLDER'] = STATIC_IMAGES

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(os.path.dirname(app.config['DATABASE']), exist_ok=True)

# ---------- Sample fallback PRODUCTS (used if DB unavailable) ----------
PRODUCTS = [
    {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray.jpeg", "excerpt": "Handpicked premium eggs, perfect for baking or daily use.", "description": "Premium eggs from free-range hens."},
    {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggtry.jpeg", "excerpt": "Fresh, organic eggs from free-range hens.", "description": "Tray of 30 fresh eggs."},
    {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen with superior egg production.", "description": "Kuroiler hens are hardy and productive."},
    {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster, excellent for breeding.", "description": "Ideal for smallholder breeding programs."},
    {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet, weaned and ready for rearing.", "description": "Weaned Landrace piglet, vaccinated and dewormed."},
    {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White breed piglet known for rapid growth.", "description": "Large White piglet, ideal for meat production."}
]

# ---------- SQLite helpers (optional) ----------
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        try:
            db = g._database = sqlite3.connect(app.config['DATABASE'])
            db.row_factory = sqlite3.Row
        except Exception:
            db = None
    return db

def query_db(query, args=(), one=False):
    db = get_db()
    if not db:
        raise RuntimeError("No database available")
    cur = db.execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

def execute_db(query, args=()):
    db = get_db()
    if not db:
        raise RuntimeError("No database available")
    cur = db.execute(query, args)
    db.commit()
    lastrowid = cur.lastrowid
    cur.close()
    return lastrowid

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# ---------- Utility ----------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_uploaded_image(file_storage):
    if not file_storage or file_storage.filename == '':
        return None
    filename = secure_filename(file_storage.filename)
    if not allowed_file(filename):
        return None
    dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file_storage.save(dest)
    return filename

# ---------- DB initialization & seed (optional) ----------
def init_db_and_seed():
    conn = sqlite3.connect(app.config['DATABASE'])
    c = conn.cursor()
    c.executescript("""
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
    c.execute("SELECT COUNT(1) FROM products")
    if c.fetchone()[0] == 0:
        seed = [
            ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs, perfect for baking.','Premium eggs from free-range hens',150,'per dozen','eggtray.jpeg'),
            ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs from free-range hens.','Tray of 30 fresh eggs',350,'per tray (30 eggs)','eggtry.jpeg'),
            ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Kuroiler hens are hardy and productive.',1100,'per bird','hen.jpeg'),
            ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Ideal for smallholder breeding programs.',1500,'per bird','cock.jpeg'),
            ('pigs','Landrace Piglet','Healthy Landrace piglet, weaned and ready.','Weaned Landrace piglet, vaccinated and dewormed.',8000,'per piglet','piglet.jpeg'),
            ('pigs','Large White Piglet','Large White breed piglet known for rapid growth.','Large White piglet, ideal for meat production.',9000,'per piglet','piglet1.jpeg'),
        ]
        c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
        conn.commit()
    conn.close()

# Create DB and seed if missing
if not os.path.exists(app.config['DATABASE']):
    init_db_and_seed()

# ---------- Context processors ----------
@app.context_processor
def inject_now():
    return {'current_year': datetime.utcnow().year}

@app.context_processor
def utility_processor():
    def endpoint_exists(name):
        return name in current_app.view_functions
    return dict(endpoint_exists=endpoint_exists)

# ---------- Routes ----------
@app.route('/')
def index():
    try:
        rows = query_db("SELECT * FROM products ORDER BY id DESC LIMIT 4")
        products = [dict(r) for r in rows] if rows else []
    except Exception:
        products = PRODUCTS[:4]
    return render_template('index.html', title="Farmyard — Kisumu's Premier Farm", products=products)

@app.route('/shop')
def shop():
    category = request.args.get('category', 'all')
    q = request.args.get('q', '').strip().lower()
    try:
        sql = "SELECT id, category, title, price, unit, image, excerpt FROM products"
        params = []
        where = []
        if category != 'all':
            where.append("category = ?")
            params.append(category)
        if q:
            where.append("LOWER(title) LIKE ?")
            params.append(f"%{q}%")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        rows = query_db(sql, params)
        products = [dict(r) for r in rows]
    except Exception:
        products = PRODUCTS.copy()
        if category != 'all':
            products = [p for p in products if p['category'] == category]
        if q:
            products = [p for p in products if q in p['title'].lower()]
    categories = ['all', 'eggs', 'poultry', 'pigs']
    return render_template('shop.html', products=products, categories=categories, active_category=category)

@app.route('/api/product/<int:product_id>')
def product_api(product_id):
    try:
        row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
        if row:
            return jsonify(dict(row))
    except Exception:
        pass
    p = next((x for x in PRODUCTS if x['id'] == product_id), None)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify(p)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    try:
        row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
        if row:
            product = dict(row)
        else:
            raise LookupError
    except Exception:
        product = next((x for x in PRODUCTS if x['id'] == product_id), None)
        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('shop'))
    return render_template('product_detail.html', product=product)

@app.route('/order/<int:product_id>', methods=['GET','POST'])
def order(product_id):
    try:
        row = query_db("SELECT title FROM products WHERE id = ?", (product_id,), one=True)
        title = row['title'] if row else None
    except Exception:
        p = next((x for x in PRODUCTS if x['id'] == product_id), None)
        title = p['title'] if p else None
    if not title:
        flash('Product not found', 'danger')
        return redirect(url_for('shop'))
    flash(f'Order request received for {title}. We will contact you shortly.', 'success')
    return redirect(url_for('product_detail', product_id=product_id))

# Admin create/edit/delete (optional)
@app.route('/admin/products/new', methods=['GET','POST'])
def product_create():
    if request.method == 'POST':
        title = request.form.get('title','').strip()
        category = request.form.get('category','eggs')
        price = int(request.form.get('price') or 0)
        unit = request.form.get('unit','per item')
        excerpt = request.form.get('excerpt','')
        description = request.form.get('description','')
        image_file = request.files.get('image')
        filename = save_uploaded_image(image_file) if image_file else None
        try:
            execute_db(
                "INSERT INTO products (category, title, excerpt, description, price, unit, image) VALUES (?,?,?,?,?,?,?)",
                (category, title, excerpt, description, price, unit, filename)
            )
            flash('Product created.', 'success')
            return redirect(url_for('shop'))
        except Exception:
            new_id = max(p['id'] for p in PRODUCTS) + 1 if PRODUCTS else 1
            PRODUCTS.append({
                "id": new_id, "category": category, "title": title, "price": price,
                "unit": unit, "image": filename or 'placeholder.jpg', "excerpt": excerpt, "description": description
            })
            flash('Product created (in-memory fallback).', 'success')
            return redirect(url_for('shop'))
    return render_template('product_form.html', action='Create', product=None)

@app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
def product_edit(product_id):
    try:
        row = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
        if not row:
            raise LookupError
        product = dict(row)
    except Exception:
        product = next((x for x in PRODUCTS if x['id'] == product_id), None)
        if not product:
            flash('Not found', 'danger')
            return redirect(url_for('shop'))
    if request.method == 'POST':
        title = request.form.get('title', product.get('title')).strip()
        category = request.form.get('category', product.get('category'))
        price = int(request.form.get('price') or product.get('price', 0))
        unit = request.form.get('unit', product.get('unit'))
        excerpt = request.form.get('excerpt', product.get('excerpt'))
        description = request.form.get('description', product.get('description'))
        image_file = request.files.get('image')
        filename = product.get('image')
        if image_file and image_file.filename:
            saved = save_uploaded_image(image_file)
            if saved:
                filename = saved
        try:
            execute_db(
                "UPDATE products SET category=?, title=?, excerpt=?, description=?, price=?, unit=?, image=? WHERE id=?",
                (category, title, excerpt, description, price, unit, filename, product_id)
            )
            flash('Product updated.', 'success')
            return redirect(url_for('product_detail', product_id=product_id))
        except Exception:
            for p in PRODUCTS:
                if p['id'] == product_id:
                    p.update({"category": category, "title": title, "excerpt": excerpt, "description": description, "price": price, "unit": unit, "image": filename})
                    break
            flash('Product updated (in-memory fallback).', 'success')
            return redirect(url_for('product_detail', product_id=product_id))
    return render_template('product_form.html', action='Edit', product=product)

@app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
def product_delete(product_id):
    try:
        execute_db("DELETE FROM products WHERE id = ?", (product_id,))
        flash('Product deleted.', 'success')
    except Exception:
        global PRODUCTS
        PRODUCTS = [p for p in PRODUCTS if p['id'] != product_id]
        flash('Product deleted (in-memory fallback).', 'success')
    return redirect(url_for('shop'))

# Simple pages
@app.route('/projects')
def projects():
    return render_template('projects.html', title="Projects — Farmyard")

@app.route('/about')
def about():
    return render_template('about.html', title="About — Farmyard")

@app.route('/contact', methods=['GET','POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')
        if not name or not email or not message:
            flash('Please fill in the required fields.', 'danger')
            return redirect(url_for('contact'))
        flash('Message sent — thank you!', 'success')
        return redirect(url_for('contact'))
    return render_template('contact.html', title="Contact — Farmyard")

@app.route('/privacy')
def privacy():
    return render_template('privacy.html', title='Privacy Policy')

@app.route('/terms')
def terms():
    return render_template('terms.html', title='Terms of Service')

# ---------- Run ----------
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)





# import os
# import sqlite3
# from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g
# from werkzeug.utils import secure_filename
# from datetime import datetime

# BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# DB_PATH = os.path.join(BASE_DIR, 'farmyard.db')
# UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'images')
# os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# app = Flask(__name__)
# app.config['SECRET_KEY'] = 'replace-with-a-secure-secret'
# app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# app.config['DATABASE'] = DB_PATH

# # -------------------------
# # SQLite helpers (no ORM)
# # -------------------------
# def get_db():
#     if 'db' not in g:
#         g.db = sqlite3.connect(app.config['DATABASE'])
#         g.db.row_factory = sqlite3.Row
#     return g.db

# @app.teardown_appcontext
# def close_db(exc):
#     db = g.pop('db', None)
#     if db is not None:
#         db.close()

# def query_db(query, args=(), one=False):
#     cur = get_db().execute(query, args)
#     rv = cur.fetchall()
#     cur.close()
#     return (rv[0] if rv else None) if one else rv

# def execute_db(query, args=()):
#     db = get_db()
#     cur = db.execute(query, args)
#     db.commit()
#     lastrowid = cur.lastrowid
#     cur.close()
#     return lastrowid

# # -------------------------
# # DB initialization & seed
# # -------------------------
# def init_db_and_seed():
#     """Create products table and seed sample products if empty."""
#     conn = sqlite3.connect(DB_PATH)
#     c = conn.cursor()
#     c.executescript("""
#     CREATE TABLE IF NOT EXISTS products (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       category TEXT NOT NULL,
#       title TEXT NOT NULL,
#       excerpt TEXT,
#       description TEXT,
#       price INTEGER NOT NULL,
#       unit TEXT,
#       image TEXT
#     );
#     """)
#     c.execute("SELECT COUNT(1) FROM products")
#     if c.fetchone()[0] == 0:
#         seed = [
#             ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs, perfect for baking.','Full description for premium dozen eggs',150,'per dozen','eggs-dozen.jpg'),
#             ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs from free-range hens.','Full description for tray',350,'per tray (30 eggs)','eggs-tray.jpg'),
#             ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Full description for Kuroiler',1100,'per bird','kuroiler.jpg'),
#             ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Full description for rooster',1500,'per bird','rhode-island-red.jpg'),
#             ('pigs','Landrace Piglet','Healthy Landrace piglet, weaned and ready.','Full description for landrace',8000,'per piglet','landrace-piglet.jpg'),
#             ('pigs','Large White Piglet','Large White breed piglet known for rapid growth.','Full description for large white',9000,'per piglet','large-white-piglet.jpg'),
#         ]
#         c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
#         conn.commit()
#     conn.close()

# # Ensure DB exists and seeded on startup
# if not os.path.exists(DB_PATH):
#     init_db_and_seed()

# # -------------------------
# # Sample fallback PRODUCTS (used only if DB access fails)
# # -------------------------
# PRODUCTS = [
#     {"id": 1, "category": "eggs", "title": "Farm Eggs – Premium Dozen", "price": 150, "unit": "per dozen", "image": "eggtray.jpeg", "excerpt": "Handpicked premium eggs, perfect for baking or daily use."},
#     {"id": 2, "category": "eggs", "title": "Fresh Farm Eggs (Tray)", "price": 350, "unit": "per tray (30 eggs)", "image": "eggtry.jpeg", "excerpt": "Fresh, organic eggs from free-range hens."},
#     {"id": 3, "category": "poultry", "title": "Kuroiler Hen", "price": 1100, "unit": "per bird", "image": "hen.jpeg", "excerpt": "Robust dual-purpose hen with superior egg production."},
#     {"id": 4, "category": "poultry", "title": "Rhode Island Red Cock", "price": 1500, "unit": "per bird", "image": "cock.jpeg", "excerpt": "Strong and healthy rooster, excellent for breeding."},
#     {"id": 5, "category": "pigs", "title": "Landrace Piglet", "price": 8000, "unit": "per piglet", "image": "piglet.jpeg", "excerpt": "Healthy Landrace piglet, weaned and ready for rearing."},
#     {"id": 6, "category": "pigs", "title": "Large White Piglet", "price": 9000, "unit": "per piglet", "image": "piglet1.jpeg", "excerpt": "Large White breed piglet known for rapid growth."}
# ]

# # -------------------------
# # Context processor
# # -------------------------
# @app.context_processor
# def inject_now():
#     return {'current_year': datetime.utcnow().year}

# # -------------------------
# # Routes
# # -------------------------
# @app.route('/')
# def index():
#     """
#     Render homepage with a safe 'products' variable for the template.
#     Tries SQLite first, falls back to the in-memory PRODUCTS list.
#     """
#     products = []
#     try:
#         rows = query_db("SELECT * FROM products ORDER BY id DESC LIMIT 4")
#         products = [dict(r) for r in rows] if rows else []
#     except Exception:
#         try:
#             products = PRODUCTS[:4]
#         except NameError:
#             products = []
#     return render_template('index.html', title="Farmyard — Kisumu's Premier Farm", products=products)

# # app.py (add once near other imports / context processors)
# from flask import current_app

# @app.context_processor
# def utility_processor():
#     def endpoint_exists(name):
#         return name in current_app.view_functions
#     return dict(endpoint_exists=endpoint_exists)


# @app.route('/shop')
# def shop():
#     category = request.args.get('category', 'all')
#     q = request.args.get('q', '').strip()
#     sql = "SELECT * FROM products"
#     params = []
#     where = []
#     if category != 'all':
#         where.append("category = ?")
#         params.append(category)
#     if q:
#         where.append("title LIKE ?")
#         params.append(f"%{q}%")
#     if where:
#         sql += " WHERE " + " AND ".join(where)
#     sql += " ORDER BY id DESC"
#     try:
#         products = query_db(sql, params)
#         products = [dict(p) for p in products]
#     except Exception:
#         products = PRODUCTS if category == 'all' else [p for p in PRODUCTS if p['category'] == category]
#     categories = ['all', 'eggs', 'poultry', 'pigs']
#     return render_template('shop.html', products=products, categories=categories, active_category=category)

# # API for modal
# @app.route('/api/product/<int:product_id>')
# def product_api(product_id):
#     try:
#         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
#         if not p:
#             return jsonify({"error":"not found"}), 404
#         return jsonify(dict(p))
#     except Exception:
#         # fallback to sample list
#         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
#         if not p:
#             return jsonify({"error":"not found"}), 404
#         return jsonify(p)

# @app.route('/product/<int:product_id>')
# def product_detail(product_id):
#     try:
#         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
#         if not p:
#             flash('Product not found.', 'danger')
#             return redirect(url_for('shop'))
#         product = dict(p)
#     except Exception:
#         product = next((x for x in PRODUCTS if x['id'] == product_id), None)
#         if not product:
#             flash('Product not found.', 'danger')
#             return redirect(url_for('shop'))
#     return render_template('product_detail.html', product=product)

# # Create product (admin)
# @app.route('/admin/products/new', methods=['GET','POST'])
# def product_create():
#     if request.method == 'POST':
#         title = request.form.get('title','').strip()
#         category = request.form.get('category','eggs')
#         price = int(request.form.get('price') or 0)
#         unit = request.form.get('unit','per item')
#         excerpt = request.form.get('excerpt','')
#         description = request.form.get('description','')
#         image_file = request.files.get('image')
#         filename = None
#         if image_file and image_file.filename:
#             filename = secure_filename(image_file.filename)
#             image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
#         execute_db(
#             "INSERT INTO products (category, title, excerpt, description, price, unit, image) VALUES (?,?,?,?,?,?,?)",
#             (category, title, excerpt, description, price, unit, filename)
#         )
#         flash('Product created.', 'success')
#         return redirect(url_for('shop'))
#     return render_template('product_form.html', action='Create', product=None)

# # Edit product (admin)
# @app.route('/admin/products/<int:product_id>/edit', methods=['GET','POST'])
# def product_edit(product_id):
#     p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
#     if not p:
#         flash('Not found', 'danger'); return redirect(url_for('shop'))
#     if request.method == 'POST':
#         title = request.form.get('title', p['title']).strip()
#         category = request.form.get('category', p['category'])
#         price = int(request.form.get('price') or p['price'])
#         unit = request.form.get('unit', p['unit'])
#         excerpt = request.form.get('excerpt', p['excerpt'])
#         description = request.form.get('description', p['description'])
#         image_file = request.files.get('image')
#         filename = p['image']
#         if image_file and image_file.filename:
#             filename = secure_filename(image_file.filename)
#             image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
#         execute_db(
#             "UPDATE products SET category=?, title=?, excerpt=?, description=?, price=?, unit=?, image=? WHERE id=?",
#             (category, title, excerpt, description, price, unit, filename, product_id)
#         )
#         flash('Product updated.', 'success')
#         return redirect(url_for('product_detail', product_id=product_id))
#     return render_template('product_form.html', action='Edit', product=dict(p))

# # Delete product (admin)
# @app.route('/admin/products/<int:product_id>/delete', methods=['POST'])
# def product_delete(product_id):
#     execute_db("DELETE FROM products WHERE id = ?", (product_id,))
#     flash('Product deleted.', 'success')
#     return redirect(url_for('shop'))

# # Contact (GET/POST)
# @app.route('/contact', methods=['GET','POST'])
# def contact():
#     if request.method == 'POST':
#         name = request.form.get('name')
#         phone = request.form.get('phone')
#         email = request.form.get('email')
#         message = request.form.get('message')
#         if not name or not email or not message:
#             flash('Please fill in the required fields.', 'danger')
#             return redirect(url_for('contact'))
#         flash('Message sent — thank you!', 'success')
#         return redirect(url_for('contact'))
#     return render_template('contact.html', title="Contact — Farmyard")

# # Simple order placeholder
# @app.route('/order/<int:product_id>', methods=['GET','POST'])
# def order(product_id):
#     try:
#         p = query_db("SELECT * FROM products WHERE id = ?", (product_id,), one=True)
#         title = p['title'] if p else None
#     except Exception:
#         p = next((x for x in PRODUCTS if x['id'] == product_id), None)
#         title = p['title'] if p else None
#     if not title:
#         flash('Product not found', 'danger')
#         return redirect(url_for('shop'))
#     flash(f'Order request received for {title}. We will contact you.', 'success')
#     return redirect(url_for('product_detail', product_id=product_id))

# # Simple pages
# @app.route('/projects')
# def projects():
#     return render_template('projects.html', title="Projects — Farmyard")

# @app.route('/about')
# def about():
#     return render_template('about.html', title="About — Farmyard")


# # app.py

# @app.route('/privacy')
# def privacy():
#     return render_template('privacy.html', title='Privacy Policy')

# @app.route('/terms')
# def terms():
#     return render_template('terms.html', title='Terms of Service')



# if __name__ == '__main__':
#     app.run(debug=True, host='0.0.0.0', port=5000)
