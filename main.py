import os
import requests
import random
import string
import time
import atexit
from flask import Flask, render_template, request, redirect, url_for, flash, abort, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# --- CONFIGURATION ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'augusta-national-2026-v3')

db_url = os.environ.get('DATABASE_URL', 'sqlite:///masters_draft.db')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- FLASK-MAIL CONFIGURATION ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'ktwom22@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', 'ktwom22@gmail.com')

# --- INITIALIZATION ---
db = SQLAlchemy(app)
mail = Mail(app)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

# --- MODELS ---

rosters = db.Table('rosters',
                   db.Column('entry_id', db.Integer, db.ForeignKey('entry.id'), primary_key=True),
                   db.Column('golfer_id', db.Integer, db.ForeignKey('golfer.id'), primary_key=True)
                   )


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    entries = db.relationship('Entry', backref='owner', lazy=True)


class League(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    invite_code = db.Column(db.String(10), unique=True, nullable=False)
    max_size = db.Column(db.Integer, default=10)
    status = db.Column(db.String(20), default='recruiting')
    is_global = db.Column(db.Boolean, default=False)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    entries = db.relationship('Entry', backref='league', lazy=True)


class Golfer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    espn_id = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100), nullable=False)
    headshot_url = db.Column(db.String(255))
    world_rank = db.Column(db.Integer, default=999)
    api_score = db.Column(db.Integer, default=0)
    manual_score = db.Column(db.Integer, nullable=True)

    @property
    def current_total(self):
        return self.manual_score if self.manual_score is not None else self.api_score


class Entry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    league_id = db.Column(db.Integer, db.ForeignKey('league.id'))
    draft_order = db.Column(db.Integer, default=0)
    golfers = db.relationship('Golfer', secondary=rosters, backref='teams')

    @property
    def combined_score(self):
        return sum([g.current_total for g in self.golfers])


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- AUTOMATED SYNC LOGIC ---

def run_sync_logic():
    """Shared function for manual and automated syncs"""
    url = "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard?event=401811941"
    try:
        data = requests.get(url).json()
        competitors = data['events'][0]['competitions'][0]['competitors']

        for p in competitors:
            espn_id = str(p['id'])
            athlete_data = p['athlete']

            g = Golfer.query.filter_by(espn_id=espn_id).first()
            if not g:
                g = Golfer(name=athlete_data['displayName'], espn_id=espn_id)
                db.session.add(g)

            g.headshot_url = athlete_data.get('headshot', {}).get('href')

            stats = p.get('statistics', [])
            tournament_total = 0
            for s in stats:
                if s.get('name') == 'scoreToPar':
                    tournament_total = int(s.get('value', 0))
                    break

            g.api_score = tournament_total
            g.world_rank = p.get('curatedRank', {}).get('current', 999)

        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        print(f"Sync Error: {str(e)}")
        return False


def scheduled_sync():
    with app.app_context():
        print("🕒 Running 15-minute background sync...")
        run_sync_logic()


# Initialize Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=scheduled_sync, trigger="interval", minutes=15)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


# --- AUTH & NAVIGATION ---

@app.route('/', methods=['GET', 'POST'])
def index():
    if current_user.is_authenticated and request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            name = request.form.get('league_name')
            size = int(request.form.get('max_size', 10))
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            new_league = League(name=name, invite_code=code, max_size=size, creator_id=current_user.id)
            db.session.add(new_league)
            db.session.flush()
            new_entry = Entry(team_name=f"{current_user.username.split('@')[0]}'s Team",
                              user_id=current_user.id, league_id=new_league.id)
            db.session.add(new_entry)
            db.session.commit()
            flash(f"League '{name}' created!")
            return redirect(url_for('index'))

        elif action == 'join':
            code = request.form.get('invite_code', '').upper()
            league = League.query.filter_by(invite_code=code).first()
            if league and len(league.entries) < league.max_size:
                new_entry = Entry(team_name=request.form.get('team_name'),
                                  user_id=current_user.id, league_id=league.id)
                db.session.add(new_entry)
                db.session.commit()
                flash(f"Joined {league.name}!")
            else:
                flash("League full or invalid code.")
            return redirect(url_for('index'))

    pro_golfers = Golfer.query.order_by(Golfer.api_score.asc(), Golfer.world_rank.asc()).all()
    user_entries = current_user.entries if current_user.is_authenticated else []
    return render_template('index.html', pro_golfers=pro_golfers, user_entries=user_entries)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        hashed_pw = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
        new_user = User(username=request.form['username'], password=hashed_pw)
        db.session.add(new_user)
        db.session.flush()

        global_league = League.query.filter_by(is_global=True).first()
        if not global_league:
            global_league = League(name="2026 Global Tournament", invite_code="MASTERS", max_size=5000, is_global=True,
                                   status='drafting')
            db.session.add(global_league)
            db.session.flush()

        global_entry = Entry(team_name=f"{new_user.username.split('@')[0]}'s Global Team",
                             user_id=new_user.id, league_id=global_league.id)
        db.session.add(global_entry)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for('index'))
    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            return redirect(url_for('index'))
        flash("Invalid credentials.")
    return render_template('login.html')


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))


# --- PASSWORD RECOVERY ---

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(username=email).first()
        if user:
            token = serializer.dumps(email, salt='pw-reset-token')
            link = url_for('reset_password_route', token=token, _external=True)
            try:
                msg = Message("⛳ Password Reset Request", recipients=[email])
                msg.body = f"Reset your password here: {link}"
                msg.html = f"<p>Reset your password here: <a href='{link}'>Reset Link</a></p>"
                mail.send(msg)
                flash("Recovery email sent.")
            except Exception as e:
                flash(f"Email error: {str(e)}")
        else:
            flash("If that email exists in our system, a reset link has been sent.")
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password_route(token):
    try:
        email = serializer.loads(token, salt='pw-reset-token', max_age=1800)
    except:
        flash("The reset link is invalid or has expired.")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        user = User.query.filter_by(username=email).first()
        if user:
            user.password = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
            db.session.commit()
            flash("Your password has been updated! You can now login.")
            return redirect(url_for('login'))
        else:
            flash("User not found.")
            return redirect(url_for('index'))
    return render_template('reset_with_token.html')


# --- LEAGUE SYSTEM ---

@app.route('/leagues')
@login_required
def leagues_dashboard():
    return redirect(url_for('index'))


@app.route('/leagues/create', methods=['POST'])
@login_required
def create_league():
    name = request.form.get('league_name')
    size = int(request.form.get('max_size', 10))
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    new_league = League(name=name, invite_code=code, max_size=size, creator_id=current_user.id)
    db.session.add(new_league)
    db.session.flush()
    new_entry = Entry(team_name=f"{current_user.username.split('@')[0]}'s Team", user_id=current_user.id,
                      league_id=new_league.id)
    db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('index'))


# --- ADMIN MANUAL TOOLS & RECOVERY ---

@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin: abort(403)
    users = User.query.all()
    leagues = League.query.all()
    golfers = Golfer.query.order_by(Golfer.name).all()
    return render_template('admin.html', users=users, leagues=leagues, golfers=golfers)


@app.route('/admin/create_user', methods=['POST'])
@login_required
def admin_create_user():
    if not current_user.is_admin: abort(403)
    username = request.form.get('username')
    password = request.form.get('password')
    if User.query.filter_by(username=username).first():
        flash("Username already exists.")
        return redirect(url_for('admin_panel'))

    hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
    new_user = User(username=username, password=hashed_pw)
    db.session.add(new_user)
    db.session.commit()
    flash(f"User {username} created successfully!")
    return redirect(url_for('admin_panel'))


@app.route('/admin/manual_assign', methods=['POST'])
@login_required
def manual_assign():
    if not current_user.is_admin: abort(403)
    user_id = request.form.get('user_id')
    league_id = request.form.get('league_id')
    golfer_ids = request.form.getlist('golfer_ids')

    entry = Entry.query.filter_by(user_id=user_id, league_id=league_id).first()
    if not entry:
        entry = Entry(team_name="Manual Team", user_id=user_id, league_id=league_id)
        db.session.add(entry)

    entry.golfers = []
    for g_id in golfer_ids[:7]:
        golfer = db.session.get(Golfer, int(g_id))
        if golfer:
            entry.golfers.append(golfer)

    db.session.commit()
    flash("Roster manually updated for the user!")
    return redirect(url_for('admin_panel'))


@app.route('/admin/manual', methods=['POST'])
@login_required
def manual_score_override():
    if not current_user.is_admin: abort(403)
    golfer_id = request.form.get('golfer_id')
    score = request.form.get('score')

    golfer = db.session.get(Golfer, int(golfer_id))
    if golfer:
        golfer.manual_score = int(score)
        db.session.commit()
        flash(f"Manual score set for {golfer.name}")
    return redirect(url_for('admin_panel'))


@app.route('/admin/nuke/<string:secret>')
def nuke_and_pave(secret):
    if secret != 'masters2026':
        abort(403)
    try:
        logout_user()
        db.session.remove()
        db.drop_all()
        db.create_all()
        flash("System Reset Successful.")
        return redirect(url_for('signup'))
    except Exception as e:
        db.session.rollback()
        return f"Reset failed: {str(e)}"


# --- DRAFT & LEADERBOARD ---

@app.route('/leagues/<int:league_id>/start', methods=['POST'])
@login_required
def start_draft(league_id):
    league = db.session.get(League, league_id)
    if league.creator_id != current_user.id: abort(403)
    entries = league.entries
    random.shuffle(entries)
    for idx, entry in enumerate(entries):
        entry.draft_order = idx
    league.status = 'drafting'
    db.session.commit()
    return redirect(url_for('draft_page', league_id=league.id))


@app.route('/draft/<int:league_id>', methods=['GET', 'POST'])
@login_required
def draft_page(league_id):
    league = db.session.get(League, league_id)
    user_entry = Entry.query.filter_by(league_id=league_id, user_id=current_user.id).first()
    if not user_entry:
        abort(404)

    if league.is_global:
        if request.method == 'POST':
            golfer_id = request.form.get('golfer_id')
            golfer = db.session.get(Golfer, golfer_id)
            if len(user_entry.golfers) >= 7:
                flash("Your team is full.")
            elif golfer in user_entry.golfers:
                flash("Already picked.")
            elif golfer:
                user_entry.golfers.append(golfer)
                db.session.commit()
            return redirect(url_for('draft_page', league_id=league.id))

        available = Golfer.query.order_by(Golfer.world_rank).all()
        return render_template('draft.html', league=league, team=user_entry, golfers=available, round="Global Pick")

    entries = Entry.query.filter_by(league_id=league_id).order_by(Entry.draft_order).all()
    num_teams = len(entries)
    total_picks = db.session.query(rosters).join(Entry).filter(Entry.league_id == league_id).count()

    if total_picks >= (num_teams * 7):
        league.status = 'active'
        db.session.commit()
        return redirect(url_for('leaderboard', league_id=league_id))

    curr_round = (total_picks // num_teams) + 1
    pick_idx = total_picks % num_teams
    turn_entry = entries[pick_idx] if curr_round % 2 != 0 else entries[num_teams - 1 - pick_idx]

    if request.method == 'POST' and current_user.id == turn_entry.user_id:
        golfer_id = request.form.get('golfer_id')
        golfer = db.session.get(Golfer, golfer_id)
        already_taken = any(g.id == golfer.id for e in league.entries for g in e.golfers)
        if golfer and not already_taken:
            turn_entry.golfers.append(golfer)
            db.session.commit()
            return redirect(url_for('draft_page', league_id=league_id))

    taken_ids = [g.id for e in league.entries for g in e.golfers]
    available = Golfer.query.filter(~Golfer.id.in_(taken_ids)).order_by(Golfer.world_rank).all()
    return render_template('draft.html', league=league, team=turn_entry, golfers=available, round=curr_round)


@app.route('/leaderboard/<int:league_id>')
@login_required
def leaderboard(league_id):
    league = db.session.get(League, league_id)
    sorted_entries = sorted(league.entries, key=lambda x: x.combined_score)
    return render_template('leaderboard.html', league=league, entries=sorted_entries)


# --- ADMIN GATE & SYNC ---

@app.route('/admin_gate/<string:secret>')
@login_required
def admin_gate(secret):
    if secret == 'masters2026':
        current_user.is_admin = True
        db.session.commit()
        return redirect(url_for('admin_panel'))
    abort(403)


@app.route('/admin/sync', methods=['POST'])
@login_required
def sync_espn():
    if not current_user.is_admin: abort(403)
    if run_sync_logic():
        flash("Tournament Totals Synced Successfully!")
    else:
        flash("Sync failed. Check logs.")
    return redirect(url_for('admin_panel'))


# --- SEO & PLAYER PAGES ---

@app.route('/golfer/<string:espn_id>')
def golfer_detail(espn_id):
    golfer = Golfer.query.filter_by(espn_id=espn_id).first_or_404()
    page_title = f"{golfer.name} - Masters 2026 Status"
    page_desc = f"Track {golfer.name}'s performance at Augusta 2026."
    return render_template('golfer_detail.html', golfer=golfer, page_title=page_title, page_desc=page_desc)


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        try:
            msg = Message(subject=request.form.get('subject'),
                          recipients=['ktwom22@gmail.com'],
                          reply_to=request.form.get('email'),
                          body=request.form.get('message'))
            mail.send(msg)
            flash("Message sent!")
        except Exception as e:
            flash(f"Error: {str(e)}")
        return redirect(url_for('contact'))
    return render_template('contact.html')


@app.route('/sitemap.xml')
def sitemap():
    pages = [{'loc': url_for('index', _external=True), 'lastmod': '2026-04-07'}]
    golfers = Golfer.query.all()
    for g in golfers:
        pages.append({'loc': url_for('golfer_detail', espn_id=g.espn_id, _external=True), 'lastmod': '2026-04-07'})
    sitemap_xml = render_template('sitemap_template.xml', pages=pages)
    response = make_response(sitemap_xml)
    response.headers["Content-Type"] = "application/xml"
    return response


@app.route('/robots.txt')
def robots_txt():
    sitemap_url = url_for('sitemap', _external=True)
    content = f"User-agent: *\nAllow: /\nDisallow: /admin/\nSitemap: {sitemap_url}"
    response = make_response(content)
    response.headers["Content-Type"] = "text/plain"
    return response


@app.route('/admin/activate/<int:league_id>')
@login_required
def activate_league(league_id):
    if not current_user.is_admin: abort(403)
    league = db.session.get(League, league_id)
    if league:
        league.status = 'active'
        db.session.commit()
        flash(f"{league.name} is now LIVE!")
    return redirect(url_for('admin_panel'))


@app.route('/admin/remove_golfer', methods=['POST'])
@login_required
def admin_remove_golfer():
    if not current_user.is_admin: abort(403)
    user_id = request.form.get('user_id')
    league_id = request.form.get('league_id')
    golfer_id = request.form.get('golfer_id')

    entry = Entry.query.filter_by(user_id=user_id, league_id=league_id).first()
    golfer = db.session.get(Golfer, int(golfer_id))

    if entry and golfer in entry.golfers:
        entry.golfers.remove(golfer)
        db.session.commit()
        flash(f"Removed {golfer.name} from team.")
    else:
        flash("Player not found on that team.")

    return redirect(url_for('admin_panel'))


@app.route('/admin/remove_user_from_league', methods=['POST'])
@login_required
def admin_remove_user_from_league():
    if not current_user.is_admin: abort(403)
    user_id = request.form.get('user_id')
    league_id = request.form.get('league_id')

    entry = Entry.query.filter_by(user_id=user_id, league_id=league_id).first()

    if entry:
        entry.golfers = []
        db.session.delete(entry)
        db.session.commit()
        flash("User successfully removed from the league.")
    else:
        flash("Entry not found.")

    return redirect(url_for('admin_panel'))


with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"DB Init failed: {e}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)