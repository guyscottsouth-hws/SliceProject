import os
from dotenv import load_dotenv
from waitress import serve
import pyodbc
import sqlite3
from flask import Flask, render_template, jsonify, request, session
from flask_wtf.csrf import CSRFProtect
from threading import Thread, Event
import time
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import List, Dict
import signal
import sys
from contextlib import contextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, time as dt_time, timedelta
from pytz import timezone
 
# Load environment variables
load_dotenv()
 
app = Flask(__name__)
 
# Configuration
class Config:
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    TESTING = os.getenv('TESTING', 'False').lower() == 'true'
    DATABASE = os.getenv('DATABASE', 'orders.db')
    SECRET_KEY = os.getenv('SECRET_KEY', 'default-secret-key')
    LOG_FILE = os.getenv('LOG_FILE', 'app.log')
    MAX_LOG_SIZE = int(os.getenv('MAX_LOG_SIZE', 10 * 1024 * 1024))  # 10 MB
    BACKUP_COUNT = int(os.getenv('BACKUP_COUNT', 5))
 
app.config.from_object(Config)
 
# Initialize CSRF protection
csrf = CSRFProtect(app)
 
# Set up logging
handler = RotatingFileHandler(Config.LOG_FILE, maxBytes=Config.MAX_LOG_SIZE, backupCount=Config.BACKUP_COUNT)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)
 
# Connection parameters for SQL Anywhere
DB_CONFIG = {
    'driver': os.getenv('SQL_ANYWHERE_DRIVER'),
    'host': os.getenv('SQL_ANYWHERE_HOST'),
    'port': os.getenv('SQL_ANYWHERE_PORT'),
    'database': os.getenv('SQL_ANYWHERE_DATABASE'),
    'username': os.getenv('SQL_ANYWHERE_USERNAME'),
    'password': os.getenv('SQL_ANYWHERE_PASSWORD'),
    'server': os.getenv('SQL_ANYWHERE_SERVER')
}
 
CONN_STR = (
    f"Driver={DB_CONFIG['driver']};"
    f"Host={DB_CONFIG['host']}:{DB_CONFIG['port']};"
    f"Server={DB_CONFIG['server']};"
    f"DatabaseName={DB_CONFIG['database']};"
    f"uid={DB_CONFIG['username']};"
    f"pwd={DB_CONFIG['password']};"
    f"INT=no;"
)
 
@dataclass
class Order:
    chk_num: str
    distribution_status: int
    collected: int = 0
    timestamp: datetime = datetime.now()
 
    @property
    def status(self) -> str:
        return 'being_prepared' if self.distribution_status == 50 else 'ready_to_collect'
 
class OrderManager:
    def __init__(self):
        self.orders: Dict[str, List[str]] = {'being_prepared': [], 'ready_to_collect': []}
        self.shutdown_event = Event()
        self.initialize_sqlite_db()
 
    def initialize_sqlite_db(self):
        with sqlite3.connect(app.config['DATABASE']) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'")
            if cursor.fetchone():
                # Table exists, check the most recent timestamp
                cursor.execute("SELECT MAX(timestamp) FROM orders")
                latest_timestamp = cursor.fetchone()[0]
                if latest_timestamp:
                    latest_timestamp = datetime.fromisoformat(latest_timestamp)
                    if datetime.now() - latest_timestamp > timedelta(hours=6):
                        self.reset_sqlite_db()
                    else:
                        app.logger.info("Recent data found. Skipping database reset.")
                else:
                    self.reset_sqlite_db()
            else:
                self.reset_sqlite_db()
 
    def reset_sqlite_db(self):
        with sqlite3.connect(app.config['DATABASE']) as conn:
            conn.execute('DROP TABLE IF EXISTS orders')
            conn.execute('''CREATE TABLE orders
                            (chk_num TEXT PRIMARY KEY, 
                             distribution_status INTEGER,
                             collected INTEGER DEFAULT 0,
                             timestamp TEXT DEFAULT CURRENT_TIMESTAMP)''')
        app.logger.info("SQLite database table 'orders' has been reset.")
 
    def update_orders(self):
        while not self.shutdown_event.is_set():
            try:
                # Fetch from SQL Anywhere
                with get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(self.get_orders_query())
                    results = cursor.fetchall()
 
                # Update SQLite
                with sqlite3.connect(app.config['DATABASE']) as sqlite_conn:
                    for row in results:
                        sqlite_conn.execute('''
                            INSERT INTO orders (chk_num, distribution_status, collected, timestamp) 
                            VALUES (?, ?, 0, CURRENT_TIMESTAMP) 
                            ON CONFLICT(chk_num) DO UPDATE SET 
                            distribution_status = excluded.distribution_status
                            WHERE distribution_status != excluded.distribution_status
                        ''', (row.chk_num, row.distribution_status))
 
                # Update in-memory orders
                self.update_in_memory_orders()
                app.logger.debug(f"Orders updated: {self.orders}")
            except Exception as e:
                app.logger.error(f"Error updating orders: {e}")
            time.sleep(5)
 
    def update_in_memory_orders(self):
        with sqlite3.connect(app.config['DATABASE']) as conn:
            cursor = conn.cursor()
            cursor.execute('''SELECT chk_num, distribution_status, collected, timestamp
                              FROM orders 
                              WHERE distribution_status IN (50, 60)''')
            results = cursor.fetchall()
 
        new_orders = {'being_prepared': [], 'ready_to_collect': []}
        for row in results:
            order = Order(row[0], row[1], row[2], datetime.fromisoformat(row[3]))
            if not order.collected:
                new_orders[order.status].append(order.chk_num)
 
        self.orders = new_orders
 
    @staticmethod
    def get_orders_query():
        return """
        SELECT chk_num, distribution_status 
        FROM micros.micros.chk_dtl 
        WHERE DATE(chk_open_date_time) = CURRENT DATE 
          AND distribution_status IN (50, 60)
        ORDER BY chk_num ASC
        """
 
order_manager = OrderManager()
 
@contextmanager
def get_db_connection():
    conn = pyodbc.connect(CONN_STR)
    try:
        yield conn
    finally:
        conn.close()
 
@app.route('/')
def index():
    return render_template('index.html', orders=order_manager.orders)
 
@app.route('/all_orders')
def all_orders():
    with sqlite3.connect(app.config['DATABASE']) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT chk_num, distribution_status, collected, timestamp FROM orders')
        orders = [{'chk_num': row[0], 'distribution_status': row[1], 'collected': row[2], 'timestamp': row[3]} for row in cursor.fetchall()]
    return render_template('all_orders.html', orders=orders)
 
@app.route('/toggle_collected', methods=['POST'])
def toggle_collected():
    chk_num = request.json['chk_num']
    with sqlite3.connect(app.config['DATABASE']) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT collected FROM orders WHERE chk_num = ?', (chk_num,))
        result = cursor.fetchone()
        if result is not None:
            current_status = result[0]
            new_status = 1 - current_status
            cursor.execute('UPDATE orders SET collected = ? WHERE chk_num = ?', (new_status, chk_num))
            conn.commit()
            order_manager.update_in_memory_orders()
            return jsonify({'success': True, 'new_status': new_status})
        else:
            return jsonify({'success': False, 'error': 'Order not found'}), 404
 
@app.route('/debug')
def debug():
    try:
        with sqlite3.connect(app.config['DATABASE']) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM orders')
            results = cursor.fetchall()
        all_orders = [{'chk_num': row[0], 'distribution_status': row[1], 'collected': row[2], 'timestamp': row[3]} for row in results]
        return jsonify(all_orders)
    except Exception as e:
        app.logger.error(f"Error in debug route: {e}")
        return jsonify({'error': str(e)}), 500
 
def signal_handler(signum, frame):
    app.logger.info('Shutting down...')
    order_manager.shutdown_event.set()
    scheduler.shutdown()
    sys.exit(0)
 
def schedule_db_reset():
    order_manager.reset_sqlite_db()
 
if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    # Initialize the scheduler with an explicit timezone
    scheduler = BackgroundScheduler(timezone=timezone('Europe/London'))
    scheduler.add_job(schedule_db_reset, 'cron', hour=6, minute=0)
    scheduler.start()
    update_thread = Thread(target=order_manager.update_orders)
    update_thread.start()
    # Use Waitress as the production WSGI server
    serve(app, host='0.0.0.0', port=8080)