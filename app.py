import os
import time
import sqlite3
import hashlib
import subprocess
import threading
import requests
import socks
import socket
import feedparser
import webbrowser
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify
import schedule
import logging
from plyer import notification

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def setup_tor_proxy():
    socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", 9055)
    socket.socket = socks.socksocket
    logger.info("Tor proxy configured")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
DATABASE = 'website_monitor.db'

def init_db():
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Create groups table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            name TEXT NOT NULL,
            last_check TIMESTAMP,
            last_hash TEXT,
            is_rss BOOLEAN DEFAULT 0,
            latest_post_id TEXT,
            latest_post_title TEXT,
            latest_post_link TEXT,
            check_frequency INTEGER DEFAULT 10,
            notify BOOLEAN DEFAULT 1,
            priority INTEGER DEFAULT 0,
            tags TEXT DEFAULT '[]',
            group_id INTEGER,
            FOREIGN KEY (group_id) REFERENCES groups (id) ON DELETE SET NULL
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            detected_at TIMESTAMP,
            post_title TEXT,
            post_link TEXT,
            change_type TEXT,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )
        ''')
        
        # Check if default groups exist, if not create them
        default_groups = [
            ('YouTube', 'YouTube channels and content creators'),
            ('Bug Bounty', 'Bug bounty program websites and feeds')
        ]
        
        for group_name, description in default_groups:
            cursor.execute('SELECT id FROM groups WHERE name = ?', (group_name,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO groups (name, description) VALUES (?, ?)', 
                             (group_name, description))
        
        conn.commit()
        logger.info("Database initialized successfully")
    except sqlite3.Error as e:
        logger.error(f"Database initialization failed: {e}")
    finally:
        conn.close()

def get_website_hash(url):
    try:
        response = requests.get(url, timeout=30)
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text' in content_type or 'html' in content_type:
            return hashlib.sha256(response.content).hexdigest()
        return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def is_valid_rss(url):
    try:
        feed = feedparser.parse(requests.get(url, timeout=10).text)
        return len(feed.entries) > 0 and hasattr(feed, 'version') and feed.version != ''
    except Exception as e:
        logger.error(f"Error validating RSS {url}: {e}")
        return False

def get_latest_rss_post(url):
    try:
        feed = feedparser.parse(requests.get(url, timeout=10).text)
        if feed.entries:
            entry = feed.entries[0]
            return {
                'id': getattr(entry, 'id', entry.link),
                'title': entry.title[:100],
                'link': entry.link,
                'published': getattr(entry, 'published', None)
            }
        return None
    except Exception as e:
        logger.error(f"Error fetching RSS post {url}: {e}")
        return None

def send_notification(title, message, url=None, priority=0):
    try:
        if subprocess.run(['pgrep', 'plasmashell']).returncode == 0:
            kdialog_cmd = [
                'kdialog', '--title', title, '--passivepopup',
                f'{message}\n<a href="{url}">Click to open</a>' if url else message,
                '10'
            ]
            subprocess.run(kdialog_cmd)
            logger.info("KDE Plasma notification sent via kdialog")
        else:
            notification.notify(
                title=title,
                message=message,
                app_name="Website Monitor",
                timeout=10
            )
            logger.info("Plyer notification sent")

        threading.Thread(target=lambda: subprocess.run(
            ['ffplay', '-nodisp', '-autoexit', os.path.expanduser('~/Music/ring.mp3')],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )).start()
        return True
    except Exception as e:
        logger.error(f"Notification failed: {e}")
        return False

def check_website(site):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    setup_tor_proxy()
    changed = False
    
    if site['is_rss']:
        latest_post = get_latest_rss_post(site['url'])
        if latest_post and latest_post['id'] != site['latest_post_id']:
            cursor.execute('''
                UPDATE websites SET 
                    last_check = ?, latest_post_id = ?, latest_post_title = ?, latest_post_link = ?
                WHERE id = ?
            ''', (datetime.now(), latest_post['id'], latest_post['title'], latest_post['link'], site['id']))
            
            cursor.execute('''
                INSERT INTO changes (website_id, detected_at, post_title, post_link, change_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (site['id'], datetime.now(), latest_post['title'], latest_post['link'], 'rss_new_post'))
            
            if site['notify']:
                send_notification(f"RSS Update: {site['name']}", 
                                latest_post['title'], 
                                latest_post['link'],
                                site['priority'])
            changed = True
    else:
        current_hash = get_website_hash(site['url'])
        if current_hash and current_hash != site['last_hash']:
            cursor.execute('''
                UPDATE websites SET last_hash = ?, last_check = ? WHERE id = ?
            ''', (current_hash, datetime.now(), site['id']))
            
            cursor.execute('''
                INSERT INTO changes (website_id, detected_at, change_type)
                VALUES (?, ?, ?)
            ''', (site['id'], datetime.now(), 'content_change'))
            
            if site['notify']:
                send_notification(f"Website Update: {site['name']}", 
                                "Content has changed",
                                site['url'],
                                site['priority'])
            changed = True
    
    if not changed:
        cursor.execute('UPDATE websites SET last_check = ? WHERE id = ?', 
                      (datetime.now(), site['id']))
    
    conn.commit()
    conn.close()
    return changed

def check_all_websites():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    websites = conn.execute('SELECT * FROM websites ORDER BY priority DESC').fetchall()
    conn.close()
    
    for site in websites:
        check_website(site)
    logger.info("Completed checking all websites")

def start_scheduler():
    schedule.every(10).minutes.do(check_all_websites)
    while True:
        schedule.run_pending()
        time.sleep(1)

@app.route('/')
def index():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    
    # Get all groups with their websites count
    groups = conn.execute('''
        SELECT g.*, COUNT(w.id) as website_count 
        FROM groups g
        LEFT JOIN websites w ON g.id = w.group_id
        GROUP BY g.id
        ORDER BY g.name
    ''').fetchall()
    
    # Get ungrouped websites count
    ungrouped_count = conn.execute('''
        SELECT COUNT(*) as count FROM websites 
        WHERE group_id IS NULL OR group_id = 0
    ''').fetchone()['count']
    
    # Get active group from query parameter or show all if not specified
    active_group_id = request.args.get('group', 'all')
    
    # Fetch websites based on selected group
    if active_group_id == 'all':
        websites = conn.execute('SELECT * FROM websites ORDER BY priority DESC, name').fetchall()
    elif active_group_id == 'ungrouped':
        websites = conn.execute('''
            SELECT * FROM websites 
            WHERE group_id IS NULL OR group_id = 0
            ORDER BY priority DESC, name
        ''').fetchall()
    else:
        websites = conn.execute('''
            SELECT * FROM websites 
            WHERE group_id = ?
            ORDER BY priority DESC, name
        ''', (active_group_id,)).fetchall()
    
    conn.close()
    
    return render_template('index.html', 
                          websites=websites, 
                          groups=groups, 
                          active_group=active_group_id,
                          ungrouped_count=ungrouped_count,
                          json=json)

@app.route('/add', methods=['POST'])
def add_website():
    url = request.form.get('url')
    name = request.form.get('name')
    frequency = int(request.form.get('frequency', 10))
    priority = int(request.form.get('priority', 0))
    tags = json.dumps(request.form.get('tags', '').split(','))
    group_id = request.form.get('group_id', None)
    
    # Convert empty string to None
    if group_id == '':
        group_id = None
        
    init_db()

    setup_tor_proxy()
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    is_rss = is_valid_rss(url)
    if is_rss:
        latest_post = get_latest_rss_post(url)
        if latest_post:
            cursor.execute('''
                INSERT INTO websites (url, name, last_check, is_rss, latest_post_id, 
                    latest_post_title, latest_post_link, check_frequency, priority, tags, group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (url, name, datetime.now(), 1, latest_post['id'], latest_post['title'],
                  latest_post['link'], frequency, priority, tags, group_id))
            
            website_id = cursor.lastrowid
            cursor.execute('''
                INSERT INTO changes (website_id, detected_at, post_title, post_link, change_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (website_id, datetime.now(), latest_post['title'], latest_post['link'], 'rss_initial'))
            
            send_notification(f"New RSS: {name}", latest_post['title'], latest_post['link'], priority)
    else:
        initial_hash = get_website_hash(url)
        cursor.execute('''
            INSERT INTO websites (url, name, last_check, last_hash, is_rss, 
                check_frequency, priority, tags, group_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (url, name, datetime.now(), initial_hash, 0, frequency, priority, tags, group_id))

    conn.commit()
    conn.close()
    
    # Redirect back to the same active group view
    active_group = request.form.get('active_group', 'all')
    return redirect(url_for('index', group=active_group))

@app.route('/delete/<int:id>')
def delete_website(id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM websites WHERE id = ?', (id,))
    cursor.execute('DELETE FROM changes WHERE website_id = ?', (id,))
    conn.commit()
    conn.close()
    
    # Redirect back to the same active group view
    active_group = request.args.get('active_group', 'all')
    return redirect(url_for('index', group=active_group))

@app.route('/toggle_notify/<int:id>')
def toggle_notify(id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('UPDATE websites SET notify = NOT notify WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    
    # Redirect back to the same active group view
    active_group = request.args.get('active_group', 'all')
    return redirect(url_for('index', group=active_group))

@app.route('/check_now/<int:id>')
def check_now(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    website = conn.execute('SELECT * FROM websites WHERE id = ?', (id,)).fetchone()
    conn.close()
    if website:
        check_website(website)
        
    # Redirect back to the same active group view
    active_group = request.args.get('active_group', 'all')
    return redirect(url_for('index', group=active_group))

@app.route('/changes/<int:id>')
def view_changes(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    website = conn.execute('SELECT * FROM websites WHERE id = ?', (id,)).fetchone()
    changes = conn.execute('SELECT * FROM changes WHERE website_id = ? ORDER BY detected_at DESC', 
                          (id,)).fetchall()
    conn.close()
    return render_template('changes.html', website=website, changes=changes, json=json)

@app.route('/open_link/<int:id>')
def open_link(id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    website = conn.execute('SELECT latest_post_link FROM websites WHERE id = ?', (id,)).fetchone()
    conn.close()
    if website and website['latest_post_link']:
        webbrowser.open(website['latest_post_link'])
        return jsonify({"success": True})
    return jsonify({"success": False})

# Group management routes
@app.route('/groups', methods=['GET'])
def list_groups():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    groups = conn.execute('SELECT * FROM groups ORDER BY name').fetchall()
    conn.close()
    return render_template('groups.html', groups=groups)

@app.route('/groups/add', methods=['POST'])
def add_group():
    name = request.form.get('name')
    description = request.form.get('description', '')
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO groups (name, description) VALUES (?, ?)', 
                  (name, description))
    conn.commit()
    conn.close()
    
    return redirect(url_for('list_groups'))

@app.route('/groups/edit/<int:id>', methods=['POST'])
def edit_group(id):
    name = request.form.get('name')
    description = request.form.get('description', '')
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('UPDATE groups SET name = ?, description = ? WHERE id = ?', 
                  (name, description, id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('list_groups'))

@app.route('/groups/delete/<int:id>', methods=['GET'])
def delete_group(id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    
    # Ungroup all websites in this group
    cursor.execute('UPDATE websites SET group_id = NULL WHERE group_id = ?', (id,))
    
    # Delete the group
    cursor.execute('DELETE FROM groups WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('list_groups'))

@app.route('/website/move', methods=['POST'])
def move_website_to_group():
    website_id = request.form.get('website_id')
    group_id = request.form.get('group_id')
    
    # Handle "None" string for ungrouped
    if group_id == "none":
        group_id = None
    
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('UPDATE websites SET group_id = ? WHERE id = ?', (group_id, website_id))
    conn.commit()
    conn.close()
    
    # Redirect back to the same active group view
    active_group = request.form.get('active_group', 'all')
    return redirect(url_for('index', group=active_group))

if __name__ == '__main__':
    init_db()
    threading.Thread(target=start_scheduler, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000)