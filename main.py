import os
import requests
import random
import string
import resend
from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer

app = Flask(__name__)

# --- CONFIGURATION ---
# Railway uses environment variables. Locally, these default to safe values.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'augusta-national-2026-v3')

# Database logic: Use PostgreSQL if on Railway, otherwise SQLite
db_url = os.environ.get('DATABASE_URL', 'sqlite:///masters_draft.db')
if db_url and db_url.startswith("postgres://"):
    # SQLAlchemy 1.4+ requires "postgresql://" instead of "postgres://"
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
resend.api_key = os.environ.get('RESEND_API_KEY')
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
    username = db.Column(db.String(100), unique=True, nullable=False)  # Use Email as Username
    password = db.Column(db.String(200), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    entries = db.relationship('Entry', backref='owner', lazy=True)


class League(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    invite_code = db.Column(db.String(10), unique=True, nullable=False)
    max_size = db.Column(db.Integer, default=10)
    status = db.Column(db.String(20), default='recruiting')
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


# --- AUTH & NAVIGATION ---

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('leagues_dashboard'))
    return render_template('index.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        hashed_pw = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
        new_user = User(username=request.form['username'], password=hashed_pw)
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return redirect(url_for('leagues_dashboard'))
    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            return redirect(url_for('leagues_dashboard'))
        flash("Invalid credentials.")
    return render_template('login.html')


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))


# --- FORGOT PASSWORD (RESEND) ---

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(username=email).first()
        if user:
            token = serializer.dumps(email, salt='pw-reset-token')
            link = url_for('reset_token', token=token, _external=True)
            resend.Emails.send({
                "from": "Masters Draft <onboarding@resend.dev>",
                "to": [email],
                "subject": "⛳ Password Reset Request",
                "html": f"""
                <div style="font-family: sans-serif; padding: 20px; border-top: 5px solid #006747;">
                    <h2>Clubhouse Recovery</h2>
                    <p>Click the link below to reset your tournament credentials:</p>
                    <a href="{link}" style="background-color: #006747; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Reset Password</a>
                    <p style="margin-top: 20px; font-size: 12px; color: #666;">This link expires in 30 minutes.</p>
                </div>
                """
            })
            flash("Check your inbox for a recovery link.")
        else:
            flash("If an account exists, a link has been sent.")
        return redirect(url_for('login'))
    return render_template('forgot_password_request.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_token(token):
    try:
        email = serializer.loads(token, salt='pw-reset-token', max_age=1800)
    except:
        flash("Link expired or invalid.")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        user = User.query.filter_by(username=email).first()
        user.password = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
        db.session.commit()
        flash("Password updated! Log in to continue.")
        return redirect(url_for('login'))
    return render_template('reset_with_token.html')


# --- LEAGUE SYSTEM ---

@app.route('/leagues')
@login_required
def leagues_dashboard():
    return render_template('leagues.html', entries=current_user.entries)


@app.route('/leagues/create', methods=['POST'])
@login_required
def create_league():
    name = request.form.get('league_name')
    size = int(request.form.get('max_size', 10))
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    new_league = League(name=name, invite_code=code, max_size=size, creator_id=current_user.id)
    db.session.add(new_league)
    db.session.flush()

    # THE FIX: Split the email to get the username only
    display_name = current_user.username.split('@')[0]

    new_entry = Entry(
        team_name=f"{display_name}'s Team",
        user_id=current_user.id,
        league_id=new_league.id
    )
    db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('leagues_dashboard'))


@app.route('/leagues/join', methods=['POST'])
@login_required
def join_league():
    code = request.form.get('invite_code').upper()
    league = League.query.filter_by(invite_code=code).first()
    if not league or len(league.entries) >= league.max_size:
        flash("League full or invalid code.")
        return redirect(url_for('leagues_dashboard'))
    new_entry = Entry(team_name=request.form.get('team_name'), user_id=current_user.id, league_id=league.id)
    db.session.add(new_entry)
    db.session.commit()
    return redirect(url_for('leagues_dashboard'))


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
    flash("The Tournament Draft has begun!")
    return redirect(url_for('draft_page', league_id=league.id))


# --- DRAFTING & LEADERBOARD ---

@app.route('/draft/<int:league_id>', methods=['GET', 'POST'])
@login_required
def draft_page(league_id):
    league = db.session.get(League, league_id)
    entries = Entry.query.filter_by(league_id=league_id).order_by(Entry.draft_order).all()
    num_teams = len(entries)
    total_picks = db.session.query(rosters).join(Entry).filter(Entry.league_id == league_id).count()

    if total_picks >= (num_teams * 6):
        league.status = 'active'
        db.session.commit()
        return redirect(url_for('leaderboard', league_id=league_id))

    curr_round = (total_picks // num_teams) + 1
    pick_idx = total_picks % num_teams
    turn_entry = entries[pick_idx] if curr_round % 2 != 0 else entries[num_teams - 1 - pick_idx]

    if request.method == 'POST':
        if current_user.id != turn_entry.user_id:
            flash("Not your turn.")
        else:
            golfer = db.session.get(Golfer, request.form.get('golfer_id'))
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
    # Sorting by total score (lowest is best in golf)
    sorted_entries = sorted(league.entries, key=lambda x: x.combined_score)
    return render_template('leaderboard.html', league=league, entries=sorted_entries)


# --- ADMIN ---

@app.route('/admin_gate/<string:secret>')
@login_required
def admin_gate(secret):
    if secret == 'masters2026':
        current_user.is_admin = True
        db.session.commit()
        return redirect(url_for('admin_panel'))
    abort(403)


@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin: abort(403)
    return render_template('admin.html')


@app.route('/admin/sync', methods=['POST'])
@login_required
def sync_espn():
    if not current_user.is_admin: abort(403)
    # 2026 Masters Leaderboard Endpoint
    url = "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard?event=401811941"

    try:
        response = requests.get(url)
        data = response.json()
        competitors = data['events'][0]['competitions'][0]['competitors']

        for p in competitors:
            athlete = p['athlete']
            espn_id = str(p['id'])

            g = Golfer.query.filter_by(espn_id=espn_id).first()
            if not g:
                g = Golfer(name=athlete['displayName'], espn_id=espn_id)
                db.session.add(g)

            # --- THE 999 FIX: DEEP RANK SEARCH ---
            # 1. Try Athlete Rankings (Official World Golf Ranking)
            # Path: athlete -> rankings -> list -> rank
            rank_found = 999
            if 'rankings' in athlete and athlete['rankings']:
                rank_found = athlete['rankings'][0].get('rank', 999)

            # 2. If still 999, try the 'curRank' (Tournament Position)
            if rank_found == 999:
                pos = p.get('curRank') or p.get('rank')
                if pos:
                    try:
                        # Strip 'T' (Tied) and convert to int
                        rank_found = int(str(pos).replace('T', '').strip())
                    except:
                        pass

            g.world_rank = rank_found

            # --- HEADSHOT & SCORE ---
            # Use the higher-res headshot if available
            g.headshot_url = athlete.get('headshot', {}).get('href')

            raw_score = p.get('score', "0")
            # Handle dictionary scores vs string scores
            if isinstance(raw_score, dict):
                score_val = raw_score.get('value', 0)
            else:
                score_val = raw_score

            # Convert "E" to 0, otherwise integer
            if str(score_val).strip().upper() == "E" or score_val is None:
                g.api_score = 0
            else:
                try:
                    g.api_score = int(score_val)
                except:
                    g.api_score = 0

        db.session.commit()
        flash("Masters Sync Successful! World Rankings updated.")
    except Exception as e:
        db.session.rollback()
        flash(f"Sync failed: {str(e)}")

    return redirect(url_for('admin_panel'))


# --- CRITICAL RAILWAY INIT ---
# This block ensures tables are created even if running via Gunicorn
with app.app_context():
    try:
        db.create_all()
        print("Database initialized.")
    except Exception as e:
        print(f"Initial DB connection failed (check your DATABASE_URL): {e}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)