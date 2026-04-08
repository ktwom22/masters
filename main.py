import os
import requests
import random
import string
from flask import Flask, render_template, request, redirect, url_for, flash, abort, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from flask_mail import Mail, Message

app = Flask(__name__)

# --- CONFIGURATION ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'augusta-national-2026-v3')

db_url = os.environ.get('DATABASE_URL', 'sqlite:///masters_draft.db')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- FLASK-MAIL CONFIGURATION ---
# Note: Use a Google App Password (16 characters) for MAIL_PASSWORD
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'ktwom22@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', 'ktwom22@gmail.com')

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
            link = url_for('reset_token', token=token, _external=True)
            try:
                msg = Message("⛳ Password Reset Request", recipients=[email])
                msg.body = f"Reset your password here: {link}"
                msg.html = f"<p>Reset your password here: <a href='{link}'>Reset Link</a></p>"
                mail.send(msg)
                flash("Recovery email sent.")
            except Exception as e:
                flash(f"Email error: {str(e)}")
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_token(token):
    try:
        email = serializer.loads(token, salt='pw-reset-token', max_age=1800)
    except:
        flash("Link expired.")
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        user = User.query.filter_by(username=email).first()
        user.password = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
        db.session.commit()
        flash("Password updated!")
        return redirect(url_for('login'))
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


# --- NUKE & PAVE (TOTAL SYSTEM RESET) ---

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

    # Identify the user's entry for THIS league
    user_entry = Entry.query.filter_by(league_id=league_id, user_id=current_user.id).first()
    if not user_entry:
        abort(404)

    # GLOBAL LEAGUE LOGIC (Pick-em style)
    if league.is_global:
        if request.method == 'POST':
            golfer_id = request.form.get('golfer_id')
            golfer = db.session.get(Golfer, golfer_id)

            # Allow up to 7 golfers, and check if THIS specific user already has this golfer
            if len(user_entry.golfers) >= 7:
                flash("Your team is full (7 golfers max).")
            elif golfer in user_entry.golfers:
                flash("You already picked this golfer.")
            elif golfer:
                user_entry.golfers.append(golfer)
                db.session.commit()
                # Auto-complete global draft if they hit 7
                if len(user_entry.golfers) == 7:
                    flash("Global team complete!")
                    return redirect(url_for('leaderboard', league_id=league.id))
            return redirect(url_for('draft_page', league_id=league.id))

        # In Global, ALL golfers are available as long as they aren't on THIS user's team
        available = Golfer.query.order_by(Golfer.world_rank).all()
        return render_template('draft.html', league=league, team=user_entry, golfers=available, round="Global Pick")

    # PRIVATE LEAGUE LOGIC (Snake Draft style)
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

    if request.method == 'POST':
        if current_user.id != turn_entry.user_id:
            flash("Wait your turn!")
        else:
            golfer_id = request.form.get('golfer_id')
            golfer = db.session.get(Golfer, golfer_id)
            already_taken = any(g.id == golfer.id for e in league.entries for g in e.golfers)

            if golfer and not already_taken:
                turn_entry.golfers.append(golfer)
                db.session.commit()
                return redirect(url_for('draft_page', league_id=league_id))
            else:
                flash("Golfer already taken or invalid.")

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


@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin: abort(403)
    return render_template('admin.html')


@app.route('/admin/sync', methods=['POST'])
@login_required
def sync_espn():
    if not current_user.is_admin: abort(403)
    url = "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard?event=401811941"
    try:
        data = requests.get(url).json()
        competitors = data['events'][0]['competitions'][0]['competitors']
        for p in competitors:
            athlete = p['athlete']
            espn_id = str(p['id'])
            g = Golfer.query.filter_by(espn_id=espn_id).first()
            if not g:
                g = Golfer(name=athlete['displayName'], espn_id=espn_id)
                db.session.add(g)
            g.world_rank = athlete.get('rankings', [{}])[0].get('rank', 999)
            g.headshot_url = athlete.get('headshot', {}).get('href')
            score_data = p.get('score', '0')
            score_str = str(score_data.get('value', '0')) if isinstance(score_data, dict) else str(score_data)
            g.api_score = int(score_str) if score_str.lstrip('-').isdigit() else 0
        db.session.commit()
        flash("Synced!")
    except:
        db.session.rollback()
    return redirect(url_for('admin_panel'))


# --- PROGRAMMATIC SEO: PLAYER PAGES ---

@app.route('/golfer/<string:espn_id>')
def golfer_detail(espn_id):
    golfer = Golfer.query.filter_by(espn_id=espn_id).first_or_404()

    # SEO logic: Create a dynamic title and description for this specific player
    page_title = f"{golfer.name} - Masters 2026 Live Score & Draft Status"
    page_desc = f"Track {golfer.name}'s live performance at Augusta National 2026. Current score: {golfer.api_score}. See which fantasy teams have drafted him."

    # Find which entries in the Global League have this golfer
    global_league = League.query.filter_by(is_global=True).first()
    owners = []
    if global_league:
        owners = Entry.query.filter(Entry.league_id == global_league.id, Entry.golfers.contains(golfer)).all()

    return render_template('golfer_detail.html',
                           golfer=golfer,
                           page_title=page_title,
                           page_desc=page_desc,
                           owners=owners)


@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name')
        user_email = request.form.get('email')
        subject = request.form.get('subject')
        message_body = request.form.get('message')

        try:
            msg = Message(
                subject=f"Contact Form: {subject}",
                recipients=['ktwom22s@gmail.com'], # Sent TO your gmail
                reply_to=user_email,               # Reply goes TO the user
                body=f"New message from {name} ({user_email}):\n\n{message_body}"
            )
            mail.send(msg)
            flash("Message sent successfully! We'll get back to you soon.")
        except Exception as e:
            flash(f"Error sending message: {str(e)}")

        return redirect(url_for('contact'))

    return render_template('contact.html')

# --- TECHNICAL SEO: DYNAMIC SITEMAP ---

@app.route('/sitemap.xml')
def sitemap():
    """Generate a real-time sitemap for search engines."""
    pages = []

    # Add static pages
    pages.append({'loc': url_for('index', _external=True), 'lastmod': '2026-04-07'})

    # Add all golfer pages
    golfers = Golfer.query.all()
    for g in golfers:
        pages.append({
            'loc': url_for('golfer_detail', espn_id=g.espn_id, _external=True),
            'lastmod': '2026-04-07'
        })

    sitemap_xml = render_template('sitemap_template.xml', pages=pages)
    response = make_response(sitemap_xml)
    response.headers["Content-Type"] = "application/xml"
    return response

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"DB Init failed: {e}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)