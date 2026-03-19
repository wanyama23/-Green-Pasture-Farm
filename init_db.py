# init_db.py
import sqlite3, os
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, 'farmyard.db')

schema = """
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
"""

seed = [
    ('eggs','Farm Eggs – Premium Dozen','Handpicked premium eggs, perfect for baking.','Full description for premium dozen eggs',150,'per dozen','eggs-dozen.jpg'),
    ('eggs','Fresh Farm Eggs (Tray)','Fresh, organic eggs from free-range hens.','Full description for tray',350,'per tray (30 eggs)','eggs-tray.jpg'),
    ('poultry','Kuroiler Hen','Robust dual-purpose hen.','Full description for Kuroiler',1100,'per bird','kuroiler.jpg'),
    ('poultry','Rhode Island Red Cock','Strong and healthy rooster.','Full description for rooster',1500,'per bird','rhode-island-red.jpg'),
    ('pigs','Landrace Piglet','Healthy Landrace piglet, weaned and ready.','Full description for landrace',8000,'per piglet','landrace-piglet.jpg'),
    ('pigs','Large White Piglet','Large White breed piglet known for rapid growth.','Full description for large white',9000,'per piglet','large-white-piglet.jpg'),
]

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.executescript(schema)
c.execute("SELECT COUNT(1) FROM products")
if c.fetchone()[0] == 0:
    c.executemany("INSERT INTO products (category,title,excerpt,description,price,unit,image) VALUES (?,?,?,?,?,?,?)", seed)
    conn.commit()
    print("Seeded products.")
else:
    print("Products already exist.")
conn.close()
