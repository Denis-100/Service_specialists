import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

TZ = ZoneInfo('Europe/Kirov')
CATEGORIES = ['Все специалисты', 'Дом и ремонт', 'Красота', 'Обучение', 'Здоровье', 'Мероприятия']


def now():
    return datetime.now(TZ).replace(tzinfo=None)


def create_app(test_config=None):
    app = Flask(__name__, static_folder='static')
    app.config.update(DATABASE=os.path.join(app.instance_path, 'svobodno.sqlite'), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=1024 * 1024)
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(exist_ok=True)
    key = Path(app.instance_path) / 'session.key'
    if not key.exists():
        try:
            with key.open('x') as f:
                f.write(secrets.token_hex(32))
            key.chmod(0o600)
        except FileExistsError:
            pass
    app.secret_key = app.config.get('SECRET_KEY') or os.environ.get('APP_SECRET') or key.read_text()

    @contextmanager
    def db():
        con = sqlite3.connect(app.config['DATABASE'], timeout=15)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            with con:
                yield con
        finally:
            con.close()

    def fail(message, status=400):
        return jsonify(error=message), status

    def auth(role=None):
        def deco(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                with db() as c:
                    user = c.execute('SELECT * FROM users WHERE id=?', (session.get('uid'),)).fetchone()
                if not user:
                    return fail('Войдите в личный кабинет', 401)
                if role and user['role'] != role:
                    return fail('Недостаточно прав', 403)
                return fn(user, *args, **kwargs)
            return wrapped
        return deco

    @app.before_request
    def csrf():
        if request.method in ('POST', 'PATCH', 'DELETE'):
            if not session.get('csrf') or request.headers.get('X-CSRF-Token') != session['csrf']:
                return fail('Обновите страницу и повторите действие', 403)
            if not request.is_json:
                return fail('Ожидается JSON', 415)
            if not isinstance(request.get_json(), dict):
                return fail('Ожидается JSON-объект')

    @app.errorhandler(400)
    def bad_request(_):
        return fail('Некорректный запрос')

    @app.route('/')
    def index():
        return send_from_directory(app.static_folder, 'index.html')

    @app.get('/api/session')
    def current():
        session.setdefault('csrf', secrets.token_hex(24))
        with db() as c:
            user = c.execute('SELECT id,name,email,role,specialist_id FROM users WHERE id=?', (session.get('uid'),)).fetchone()
        return jsonify(user=dict(user) if user else None, csrf=session['csrf'])

    @app.post('/api/login')
    def login():
        data = request.get_json()
        with db() as c:
            user = c.execute('SELECT * FROM users WHERE email=?', (str(data.get('email', '')).strip().lower(),)).fetchone()
        if not user or not check_password_hash(user['password'], str(data.get('password', ''))):
            return fail('Неверная почта или пароль', 401)
        session.clear()
        session.update(uid=user['id'], csrf=secrets.token_hex(24))
        return current()

    @app.post('/api/register')
    def register():
        d = request.get_json()
        name, email, password = (str(d.get(k, '')).strip() for k in ('name', 'email', 'password'))
        if not 2 <= len(name) <= 80 or '@' not in email or len(email) > 120 or not 8 <= len(password) <= 128:
            return fail('Укажите имя, корректную почту и пароль от 8 до 128 символов')
        try:
            with db() as c:
                uid = c.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',
                                (name, email.lower(), generate_password_hash(password), 'client')).lastrowid
        except sqlite3.IntegrityError:
            return fail('Эта почта уже зарегистрирована', 409)
        session.clear()
        session.update(uid=uid, csrf=secrets.token_hex(24))
        return current()

    @app.post('/api/logout')
    def logout():
        session.clear()
        return current()

    @app.get('/api/catalog')
    def catalog():
        with db() as c:
            specialists = []
            for row in c.execute('SELECT * FROM specialists WHERE active=1 ORDER BY id'):
                s = dict(row)
                s['services'] = [dict(r) for r in c.execute('SELECT * FROM services WHERE specialist_id=? AND active=1', (s['id'],))]
                s['reviews'] = [dict(r) for r in c.execute('SELECT r.*,u.name FROM reviews r JOIN users u ON r.user_id=u.id WHERE r.specialist_id=? AND r.status="approved" ORDER BY r.id DESC', (s['id'],))]
                s['rating'] = round(sum(r['rating'] for r in s['reviews']) / len(s['reviews']), 1) if s['reviews'] else None
                s['next_slot'] = c.execute('SELECT MIN(starts_at) FROM slots WHERE specialist_id=? AND starts_at>? AND active=1', (s['id'], now().isoformat(timespec='minutes'))).fetchone()[0]
                specialists.append(s)
        return jsonify(specialists=specialists, categories=CATEGORIES, timezone='Europe/Kirov')

    @app.get('/api/slots/<int:specialist_id>')
    def slots(specialist_id):
        with db() as c:
            rows = c.execute('''SELECT s.*,v.title service,v.duration,v.price,v.mode,
                s.capacity-COALESCE((SELECT SUM(b.quantity) FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed'),0) remaining
                FROM slots s JOIN services v ON s.service_id=v.id JOIN specialists p ON p.id=s.specialist_id
                WHERE s.specialist_id=? AND s.active=1 AND v.active=1 AND p.active=1 AND s.starts_at>?
                ORDER BY s.starts_at''', (specialist_id, now().isoformat(timespec='minutes'))).fetchall()
        return jsonify(slots=[dict(r) for r in rows])

    @app.post('/api/bookings')
    @auth('client')
    def book(user):
        d = request.get_json()
        qty = d.get('quantity', 1)
        if type(qty) is not int or not 1 <= qty <= 50:
            return fail('Количество мест должно быть от 1 до 50')
        address, note = str(d.get('address', '')).strip(), str(d.get('note', '')).strip()
        if len(address) > 250 or len(note) > 1000:
            return fail('Слишком длинный адрес или комментарий')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            slot = c.execute('''SELECT s.*,v.mode,v.price,v.active service_active,p.active specialist_active
                              FROM slots s JOIN services v ON v.id=s.service_id JOIN specialists p ON p.id=s.specialist_id WHERE s.id=?''', (d.get('slot_id'),)).fetchone()
            if not slot or not slot['active'] or not slot['service_active'] or not slot['specialist_active'] or slot['starts_at'] <= now().isoformat(timespec='minutes'):
                return fail('Это время больше недоступно', 409)
            if slot['mode'] == 'Выезд' and len(address) < 5:
                return fail('Укажите адрес выезда специалиста')
            used = c.execute('SELECT COALESCE(SUM(quantity),0) FROM bookings WHERE slot_id=? AND status="confirmed"', (slot['id'],)).fetchone()[0]
            if used + qty > slot['capacity']:
                return fail('Недостаточно свободных мест. Выберите другой слот.', 409)
            if c.execute('SELECT id FROM bookings WHERE slot_id=? AND user_id=? AND status="confirmed"', (slot['id'], user['id'])).fetchone():
                return fail('Вы уже записаны на это время', 409)
            ident = c.execute('INSERT INTO bookings(user_id,slot_id,quantity,address,note,status,created_at,total) VALUES(?,?,?,?,?,"confirmed",?,?)',
                              (user['id'], slot['id'], qty, address, note, now().isoformat(timespec='minutes'), slot['price'] * qty)).lastrowid
        return jsonify(id=ident), 201

    @app.get('/api/dashboard')
    @auth()
    def dashboard(user):
        where, params = ('b.user_id=?', (user['id'],)) if user['role'] == 'client' else ('s.specialist_id=?', (user['specialist_id'],)) if user['role'] == 'master' else ('1=1', ())
        with db() as c:
            bookings = c.execute(f'''SELECT b.*,s.starts_at,s.specialist_id,s.capacity,v.title service,v.mode,v.duration,p.name specialist,p.photo,u.name client
                FROM bookings b JOIN slots s ON s.id=b.slot_id JOIN services v ON v.id=s.service_id
                JOIN specialists p ON p.id=s.specialist_id JOIN users u ON u.id=b.user_id WHERE {where} ORDER BY s.starts_at DESC''', params).fetchall()
            searches = c.execute('SELECT * FROM searches WHERE user_id=? ORDER BY id DESC LIMIT 30', (user['id'],)).fetchall()
            reviews = c.execute('SELECT r.*,p.name specialist,u.name author FROM reviews r JOIN specialists p ON p.id=r.specialist_id JOIN users u ON u.id=r.user_id ' + ('' if user['role'] == 'admin' else 'WHERE r.user_id=? ') + 'ORDER BY r.id DESC', () if user['role'] == 'admin' else (user['id'],)).fetchall()
        return jsonify(bookings=[dict(r) for r in bookings], searches=[dict(r) for r in searches], reviews=[dict(r) for r in reviews])

    @app.post('/api/bookings/<int:ident>/cancel')
    @auth()
    def cancel(user, ident):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            b = c.execute('SELECT b.*,s.specialist_id,s.starts_at FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?', (ident,)).fetchone()
            if not b:
                return fail('Запись не найдена', 404)
            if not (user['role'] == 'admin' or user['role'] == 'client' and b['user_id'] == user['id'] or user['role'] == 'master' and b['specialist_id'] == user['specialist_id']):
                return fail('Недостаточно прав', 403)
            if b['status'] != 'confirmed' or b['starts_at'] <= now().isoformat(timespec='minutes'):
                return fail('Можно отменить только предстоящую активную запись', 409)
            c.execute('UPDATE bookings SET status="cancelled" WHERE id=?', (ident,))
        return jsonify(ok=True)

    @app.post('/api/bookings/<int:ident>/complete')
    @auth('master')
    def complete(user, ident):
        with db() as c:
            b = c.execute('SELECT b.*,s.specialist_id,s.starts_at FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?', (ident,)).fetchone()
            if not b or b['specialist_id'] != user['specialist_id']:
                return fail('Запись не найдена', 404)
            if b['status'] != 'confirmed' or b['starts_at'] > now().isoformat(timespec='minutes'):
                return fail('Завершить можно только начавшуюся встречу', 409)
            c.execute('UPDATE bookings SET status="completed" WHERE id=?', (ident,))
        return jsonify(ok=True)

    @app.post('/api/reviews')
    @auth('client')
    def review(user):
        d = request.get_json()
        rating, content = d.get('rating'), str(d.get('content', '')).strip()
        if type(rating) is not int or not 1 <= rating <= 5 or not 5 <= len(content) <= 1500:
            return fail('Оцените от 1 до 5 и напишите отзыв от 5 до 1500 символов')
        with db() as c:
            b = c.execute('SELECT b.*,s.specialist_id FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=? AND b.user_id=? AND b.status="completed"', (d.get('booking_id'), user['id'])).fetchone()
            if not b:
                return fail('Отзыв доступен после завершённой встречи', 403)
            try:
                c.execute('INSERT INTO reviews(user_id,specialist_id,booking_id,rating,content,status,created_at) VALUES(?,?,?,?,?,"pending",?)', (user['id'], b['specialist_id'], b['id'], rating, content, now().date().isoformat()))
            except sqlite3.IntegrityError:
                return fail('Вы уже оставили отзыв об этой встрече', 409)
        return jsonify(ok=True), 201

    @app.post('/api/searches')
    @auth('client')
    def search_history(user):
        query = str(request.get_json().get('query', '')).strip()[:150]
        if query:
            with db() as c:
                c.execute('INSERT INTO searches(user_id,query,created_at) VALUES(?,?,?)', (user['id'], query, now().isoformat(timespec='minutes')))
        return jsonify(ok=True)

    @app.post('/api/consult')
    def consult():
        message = str(request.get_json().get('message', '')).strip().lower()
        if not 1 <= len(message) <= 1000:
            return fail('Опишите задачу в пределах 1000 символов')
        rules = [(['кран', 'теч', 'труба', 'сантех', 'унитаз', 'засор'], 1, 'Похоже, вам нужен сантехник. Если вода течёт, по возможности перекройте её подачу. Алексей поможет найти причину и устранить протечку.'),
                 (['розет', 'электр', 'провод', 'свет'], 2, 'Рекомендую электрика. Не касайтесь оголённых проводов. При запахе гари отключите питание, если это безопасно, и обратитесь в аварийную службу.'),
                 (['стриж', 'волос', 'уклад', 'красот'], 3, 'С новой стрижкой и уходом за волосами поможет стилист Мария.'),
                 (['англий', 'язык', 'экзамен', 'репетитор'], 4, 'Для занятий английским рекомендую Анну. Можно выбрать индивидуальный урок или разговорный клуб.'),
                 (['массаж', 'спин', 'мышц'], 5, 'По расслабляющему массажу можно обратиться к Дмитрию. При боли или травме сначала обратитесь к врачу: этот помощник не ставит диагнозов.'),
                 (['семинар', 'фото', 'съём', 'мастер-класс'], 6, 'Елена проводит фотосессии и групповые мастер-классы. При записи на мастер-класс выберите количество мест.')]
        if any(x in message for x in ['газ', 'пожар', 'задыха', 'груд', 'искрит']):
            return jsonify(reply='Это может быть экстренная ситуация. При непосредственной опасности звоните 112. Обычная запись к специалисту не заменяет экстренную помощь.', specialist_id=None)
        for words, ident, reply in rules:
            if any(w in message for w in words):
                with db() as c:
                    active = c.execute('SELECT active FROM specialists WHERE id=?', (ident,)).fetchone()
                if active and active['active']:
                    return jsonify(reply=reply, specialist_id=ident)
                return jsonify(reply='Специалист этой категории сейчас недоступен. Попробуйте поискать в каталоге позже.', specialist_id=None)
        return jsonify(reply='Расскажите чуть подробнее: нужна помощь с ремонтом дома, стрижкой, английским, массажем или фотографией? Я подберу категорию и предложу специалиста.', specialist_id=None)

    @app.get('/api/master')
    @auth('master')
    def master(user):
        with db() as c:
            services = c.execute('SELECT * FROM services WHERE specialist_id=?', (user['specialist_id'],)).fetchall()
            slots = c.execute('''SELECT s.*,v.title service,COALESCE((SELECT SUM(quantity) FROM bookings WHERE slot_id=s.id AND status='confirmed'),0) booked
                FROM slots s JOIN services v ON v.id=s.service_id WHERE s.specialist_id=? AND s.starts_at>? ORDER BY s.starts_at''', (user['specialist_id'], now().isoformat(timespec='minutes'))).fetchall()
        return jsonify(services=[dict(r) for r in services], slots=[dict(r) for r in slots])

    @app.post('/api/master/services')
    @auth('master')
    def add_service(user):
        d = request.get_json()
        title, mode = str(d.get('title', '')).strip(), d.get('mode')
        price, duration = d.get('price'), d.get('duration')
        if not 3 <= len(title) <= 100 or mode not in ['Выезд', 'У специалиста', 'Онлайн', 'Группа'] or type(price) is not int or not 0 <= price <= 1000000 or type(duration) is not int or not 15 <= duration <= 480:
            return fail('Проверьте название, формат, стоимость и длительность (15–480 минут)')
        with db() as c:
            c.execute('INSERT INTO services(specialist_id,title,price,duration,mode) VALUES(?,?,?,?,?)', (user['specialist_id'], title, price, duration, mode))
        return jsonify(ok=True), 201

    @app.post('/api/master/slots')
    @auth('master')
    def add_slot(user):
        d = request.get_json()
        try:
            starts = datetime.fromisoformat(d.get('starts_at', ''))
            if starts.tzinfo is not None:
                raise ValueError()
        except (ValueError, TypeError):
            return fail('Укажите корректные дату и время')
        cap = d.get('capacity', 1)
        if type(cap) is not int or not 1 <= cap <= 50 or starts <= now() or starts > now() + timedelta(days=365):
            return fail('Дата должна быть в ближайшем году, количество мест — от 1 до 50')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            service = c.execute('SELECT * FROM services WHERE id=? AND specialist_id=? AND active=1', (d.get('service_id'), user['specialist_id'])).fetchone()
            if not service:
                return fail('Услуга не найдена', 404)
            if service['mode'] != 'Группа' and cap != 1:
                return fail('Несколько мест доступны только для групповой услуги')
            end = starts + timedelta(minutes=service['duration'])
            existing = c.execute('SELECT s.starts_at,v.duration FROM slots s JOIN services v ON v.id=s.service_id WHERE s.specialist_id=? AND s.active=1', (user['specialist_id'],)).fetchall()
            if any(starts < datetime.fromisoformat(r['starts_at']) + timedelta(minutes=r['duration']) and end > datetime.fromisoformat(r['starts_at']) for r in existing):
                return fail('Этот интервал пересекается с другим слотом', 409)
            c.execute('INSERT INTO slots(specialist_id,service_id,starts_at,capacity) VALUES(?,?,?,?)', (user['specialist_id'], service['id'], starts.isoformat(timespec='minutes'), cap))
        return jsonify(ok=True), 201

    @app.post('/api/master/slots/<int:ident>/close')
    @auth('master')
    def close_slot(user, ident):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            slot = c.execute('SELECT * FROM slots WHERE id=? AND specialist_id=?', (ident, user['specialist_id'])).fetchone()
            if not slot:
                return fail('Слот не найден', 404)
            if c.execute('SELECT id FROM bookings WHERE slot_id=? AND status="confirmed"', (ident,)).fetchone():
                return fail('Сначала отмените активные записи на этот слот', 409)
            c.execute('UPDATE slots SET active=0 WHERE id=?', (ident,))
        return jsonify(ok=True)

    @app.get('/api/admin')
    @auth('admin')
    def admin(user):
        with db() as c:
            return jsonify(specialists=[dict(r) for r in c.execute('SELECT * FROM specialists')],
                           services=[dict(r) for r in c.execute('SELECT v.*,p.name specialist FROM services v JOIN specialists p ON p.id=v.specialist_id')])

    @app.post('/api/admin/moderate')
    @auth('admin')
    def moderate(user):
        d = request.get_json()
        kind, ident = d.get('kind'), d.get('id')
        with db() as c:
            if kind == 'review' and d.get('status') in ['approved', 'rejected']:
                cur = c.execute('UPDATE reviews SET status=? WHERE id=?', (d['status'], ident))
            elif kind in ['specialist', 'service'] and type(d.get('active')) is bool:
                table = 'specialists' if kind == 'specialist' else 'services'
                cur = c.execute(f'UPDATE {table} SET active=? WHERE id=?', (int(d['active']), ident))
            else:
                return fail('Некорректное действие модерации')
            if not cur.rowcount:
                return fail('Объект не найден', 404)
        return jsonify(ok=True)

    init_db(app, db)
    return app


def init_db(app, db):
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS specialists(id INTEGER PRIMARY KEY,name TEXT,profession TEXT,category TEXT,experience INTEGER,education TEXT,skills TEXT,bio TEXT,photo TEXT,location TEXT,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT,email TEXT UNIQUE,password TEXT,role TEXT,specialist_id INTEGER REFERENCES specialists(id));
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY,specialist_id INTEGER REFERENCES specialists(id),title TEXT,price INTEGER,duration INTEGER,mode TEXT,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS slots(id INTEGER PRIMARY KEY,specialist_id INTEGER REFERENCES specialists(id),service_id INTEGER REFERENCES services(id),starts_at TEXT,capacity INTEGER CHECK(capacity>0),active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),slot_id INTEGER REFERENCES slots(id),quantity INTEGER CHECK(quantity>0),address TEXT,note TEXT,status TEXT,created_at TEXT,total INTEGER);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),specialist_id INTEGER REFERENCES specialists(id),booking_id INTEGER UNIQUE REFERENCES bookings(id),rating INTEGER CHECK(rating BETWEEN 1 AND 5),content TEXT,status TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS searches(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),query TEXT,created_at TEXT);
        CREATE INDEX IF NOT EXISTS booking_slot ON bookings(slot_id,status);
        CREATE INDEX IF NOT EXISTS slot_specialist ON slots(specialist_id,starts_at);
        ''')
        if c.execute('SELECT COUNT(*) FROM specialists').fetchone()[0]:
            return
        people = [
            ('Алексей Морозов', 'Сантехник', 'Дом и ремонт', 12, 'Колледж архитектуры и строительства, инженерные системы', 'Сантехника,Поиск протечек,Монтаж оборудования', 'Помогу вернуть дому комфорт. Аккуратно устраняю протечки, устанавливаю сантехнику и всегда объясняю, что и зачем делаю.', 'photo-1500648767791-00dcc994a43e', 'Киров · выезд по городу'),
            ('Михаил Волков', 'Электрик', 'Дом и ремонт', 9, 'ВятГУ, электроэнергетика', 'Электромонтаж,Диагностика,Умный дом', 'От одной розетки до домашнего освещения. Работаю внимательно и оставляю после себя порядок.', 'photo-1560250097-0b93528c311a', 'Киров · выезд по городу'),
            ('Мария Соколова', 'Стилист по волосам', 'Красота', 7, 'Академия парикмахерского искусства, колористика', 'Стрижки,Укладки,Уход за волосами', 'Нахожу форму и стиль, которые подходят именно вам. Спокойная атмосфера, внимание к деталям и вашим пожеланиям.', 'photo-1580489944761-15a19d654956', 'Киров · ул. Ленина, 54'),
            ('Анна Лебедева', 'Преподаватель английского', 'Обучение', 8, 'ВятГУ, лингвистика; сертификат CELTA', 'Разговорный английский,Подготовка к экзаменам,Speaking club', 'Помогаю заговорить уверенно. Живые темы, понятные объяснения и занятия в вашем темпе — онлайн и в небольших группах.', 'photo-1438761681033-6461ffad8d80', 'Онлайн · Киров'),
            ('Дмитрий Орлов', 'Массажист', 'Здоровье', 6, 'Кировский медицинский колледж, курсы оздоровительного массажа', 'Расслабляющий массаж,Восстановление,Забота о теле', 'Бережный оздоровительный массаж для отдыха после насыщенной недели. При медицинских жалобах рекомендую консультацию врача.', 'photo-1519085360753-af0119f7cbe7', 'Киров · ул. Карла Маркса, 18'),
            ('Елена Павлова', 'Фотограф', 'Мероприятия', 10, 'Школа фотографии, портрет и композиция', 'Портреты,Семейные съёмки,Мастер-классы', 'Сохраняю настоящие эмоции в кадре и учу видеть красоту в повседневном. Приглашаю на съёмки и практические мастер-классы.', 'photo-1494790108377-be9c29b29330', 'Киров · студия «Свет»')]
        for i, p in enumerate(people, 1):
            c.execute('INSERT INTO specialists(id,name,profession,category,experience,education,skills,bio,photo,location) VALUES(?,?,?,?,?,?,?,?,?,?)', (i, *p[:7], '/static/photos/' + str(i) + '.jpg', p[8]))
        for name, email, role, sid in [('Александра Смирнова', 'client@svobodno.demo', 'client', None), ('Алексей Морозов', 'master@svobodno.demo', 'master', 1), ('Администратор', 'admin@svobodno.demo', 'admin', None), ('Анна Лебедева', 'teacher@svobodno.demo', 'master', 4), ('Иван Петров', 'ivan@svobodno.demo', 'client', None)]:
            c.execute('INSERT INTO users(name,email,password,role,specialist_id) VALUES(?,?,?,?,?)', (name, email, generate_password_hash('Demo2026!'), role, sid))
        services = [(1, 'Устранение протечки', 1500, 60, 'Выезд'), (1, 'Установка смесителя', 2200, 90, 'Выезд'), (2, 'Диагностика электрики', 1800, 60, 'Выезд'), (2, 'Установка розетки', 1200, 60, 'Выезд'), (3, 'Стрижка и укладка', 2500, 90, 'У специалиста'), (3, 'Вечерняя укладка', 2000, 60, 'У специалиста'), (4, 'Английский индивидуально', 1600, 60, 'Онлайн'), (4, 'Разговорный клуб', 700, 90, 'Группа'), (5, 'Расслабляющий массаж', 2800, 60, 'У специалиста'), (6, 'Портретная фотосессия', 5000, 60, 'У специалиста'), (6, 'Мастер-класс по фотографии', 1200, 120, 'Группа')]
        c.executemany('INSERT INTO services(specialist_id,title,price,duration,mode) VALUES(?,?,?,?,?)', services)
        for service in c.execute('SELECT * FROM services').fetchall():
            # Each specialist's different services occupy separate times.
            hour = 10 if service['id'] in [1, 3, 5, 7, 9, 10] else 14
            for day in range(1, 15):
                start = (now() + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
                c.execute('INSERT INTO slots(specialist_id,service_id,starts_at,capacity) VALUES(?,?,?,?)', (service['specialist_id'], service['id'], start.isoformat(timespec='minutes'), 6 if service['mode'] == 'Группа' else 1))
        for sid, service_id in [(1, 1), (3, 5), (4, 7), (5, 9), (6, 10), (2, 3)]:
            start = (now() - timedelta(days=7)).replace(hour=10, minute=0).isoformat(timespec='minutes')
            slot_id = c.execute('INSERT INTO slots(specialist_id,service_id,starts_at,capacity) VALUES(?,?,?,1)', (sid, service_id, start)).lastrowid
            booking_id = c.execute('INSERT INTO bookings(user_id,slot_id,quantity,address,note,status,created_at,total) VALUES(?,?,1,?,?,"completed",?,?)', (1 if sid == 1 else 5, slot_id, 'Киров, ул. Московская, 10', 'Демонстрационная завершённая запись', start, services[service_id-1][2])).lastrowid
            if sid != 1:
                c.execute('INSERT INTO reviews(user_id,specialist_id,booking_id,rating,content,status,created_at) VALUES(?,?,?,?,?,"approved",?)', (5, sid, booking_id, 5, 'Всё прошло отлично! Внимательное отношение, пунктуальность и отличный результат. Рекомендую.', start[:10]))
        c.execute('INSERT INTO searches(user_id,query,created_at) VALUES(1,?,?)', ('Сантехник', now().isoformat(timespec='minutes')))


if __name__ == '__main__':
    create_app().run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
