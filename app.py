from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user,
    UserMixin,
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import date, datetime
import re

from PIL import Image
import pytesseract

# ---- TESSERACT PATH (Windows) ----
# Change this path if Tesseract is installed somewhere else.
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

app = Flask(__name__)

# ---------------- CONFIG ----------------
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///expiry.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = "change_this_secret_key"

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ---------------- MODELS ----------------

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50))
    purchase_date = db.Column(db.Date, nullable=False)
    expiry_date = db.Column(db.Date, nullable=False)
    quantity = db.Column(db.String(20))
    notes = db.Column(db.Text)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user = db.relationship("User", backref=db.backref("items", lazy=True))

    @property
    def days_left(self):
        return (self.expiry_date - date.today()).days

    @property
    def status(self):
        d = self.days_left
        if d < 0:
            return "expired"
        elif d <= 3:
            return "expiring_soon"
        else:
            return "safe"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------- HELPERS ----------------

def guess_category(name: str) -> str:
    """Guess food category from name (auto, user doesn't choose)."""
    n = name.lower()
    if any(x in n for x in ["milk", "curd", "cheese", "butter", "paneer", "yogurt", "ghee"]):
        return "Dairy"
    if any(x in n for x in ["apple", "banana", "mango", "grape", "orange", "berry"]):
        return "Fruit"
    if any(x in n for x in ["tomato", "potato", "onion", "carrot", "beans", "spinach"]):
        return "Vegetable"
    if any(x in n for x in ["bread", "bun", "cake", "biscuit", "cookie"]):
        return "Bakery"
    if any(x in n for x in ["chicken", "meat", "fish", "egg"]):
        return "Meat / Protein"
    if any(x in n for x in ["juice", "soda", "cola", "drink", "milkshake"]):
        return "Beverage"
    if any(x in n for x in ["chips", "namkeen", "snack", "popcorn"]):
        return "Snacks"
    return "Other"


def extract_expiry_from_image(file_storage):
    """
    Use Tesseract OCR to scan an uploaded image and try to detect an expiry date.

    Smarter logic:
    - Scan line by line
    - If a line has "EXP", "EXPIRY", "BEST BEFORE", "USE BY" -> treat its date as expiry
    - If a line has "MFG", "MFD", "MANUFACTURED", "PACKED" -> treat its date as manufacture
    - Prefer expiry dates; otherwise, pick a date that is later than manufacture date;
      otherwise, fall back to the latest date found.
    Returns a date object or None.
    """
    try:
        image = Image.open(file_storage.stream)
        # you could tweak the image here if needed (grayscale, etc.)
        text = pytesseract.image_to_string(image)

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return None

        date_pattern = r"\d{1,4}[./\-]\d{1,2}[./\-]\d{2,4}"

        expiry_keywords = [
            "exp", "expiry", "exp.", "use by", "use before",
            "best before", "bb", "best-before"
        ]
        manufacture_keywords = [
            "mfg", "mfd", "manufactured", "mfg.", "mfd.",
            "packed on", "pkd", "pkd.", "packed"
        ]

        date_formats = [
            "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
            "%d/%m/%y", "%d-%m-%y",
            "%Y-%m-%d", "%Y/%m/%d",
        ]

        def parse_date_str(s: str):
            for fmt in date_formats:
                try:
                    d = datetime.strptime(s, fmt).date()
                    if d.year < 2000:
                        d = d.replace(year=d.year + 2000)
                    return d
                except ValueError:
                    continue
            return None

        expiry_dates = []
        manufacture_dates = []
        unknown_dates = []

        for line in lines:
            lower = line.lower()
            candidates = re.findall(date_pattern, line)
            if not candidates:
                continue

            for cand in candidates:
                d = parse_date_str(cand)
                if not d:
                    continue

                if any(k in lower for k in expiry_keywords):
                    expiry_dates.append(d)
                elif any(k in lower for k in manufacture_keywords):
                    manufacture_dates.append(d)
                else:
                    unknown_dates.append(d)

        # 1) If we found explicit expiry dates, pick the latest one
        if expiry_dates:
            return max(expiry_dates)

        # 2) If we have manufacture date(s) and unknown dates, choose unknown date AFTER manufacture
        if manufacture_dates and unknown_dates:
            mfg_ref = min(manufacture_dates)
            later_unknown = [d for d in unknown_dates if d > mfg_ref]
            if later_unknown:
                # earliest date AFTER manufacture is safest as expiry
                return min(later_unknown)

        # 3) Otherwise, if only unknown dates, pick the latest (likely expiry)
        if unknown_dates:
            return max(unknown_dates)

        # 4) If only manufacture dates exist, return the latest
        if manufacture_dates:
            return max(manufacture_dates)

        return None

    except Exception as e:
        print("OCR error:", e)
        return None

    except Exception as e:
        print("OCR error:", e)
        return None


def suggest_recipes_for_item(item):
    """
    Simple recipe suggestions based on item name / category.
    Used when item is expiring soon.
    """
    name = item.name.lower()
    cat = (item.category or "").lower()
    recipes = []

    if "milk" in name or "curd" in name or "paneer" in name or cat == "dairy":
        recipes = [
            "Paneer bhurji with roti",
            "Fruit custard with leftover milk",
            "Curd rice with tadka",
        ]
    elif "bread" in name or cat == "bakery":
        recipes = [
            "Veg sandwich with toasted bread",
            "South Indian style bread upma",
            "Garlic bread bites in air fryer",
        ]
    elif "banana" in name or "apple" in name or cat == "fruit":
        recipes = [
            "Banana smoothie / milkshake",
            "Fruit salad with honey and nuts",
            "Caramelised fruit over oats or pancakes",
        ]
    elif "tomato" in name or "potato" in name or cat == "vegetable":
        recipes = [
            "Mixed veg curry with chapati",
            "Roasted veggies with herbs and garlic",
            "Simple veg pulao with leftover veggies",
        ]
    elif "chicken" in name or "egg" in name or cat.startswith("meat"):
        recipes = [
            "Egg bhurji / masala omelette",
            "Quick chicken stir fry with veggies",
            "Chicken fried rice using leftover rice",
        ]
    else:
        recipes = [
            "Quick mixed veg stir fry with whatever you have",
            "One-pot pulao using leftovers",
            "Cheesy baked leftovers in oven/air fryer",
        ]
    return recipes


def ask_chatbot(message: str) -> str:
    """
    Offline rule-based chatbot.
    No API key, no internet needed.
    It can also look into your DB to answer expiry questions.
    """
    text = message.lower().strip()
    if not text:
        return "Say something about expiry, storage, or recipes and I'll try to help 💗"

    # greetings
    if any(w in text for w in ["hi", "hello", "hey"]):
        return "Hi! I'm your little food-buddy bot 🥝. Ask me about expiry tips, storage, or what to cook before things go bad."

    # storage tips
    if "store" in text or "storage" in text or "keep" in text:
        return (
            "General tips: keep dairy in the coldest part of the fridge (not the door), "
            "use airtight containers for leftovers, and don't overcrowd the fridge so air can circulate 🧊."
        )

    # expiry summary using DB
    if "expiry" in text or "expire" in text or "expiring" in text:
        if current_user.is_authenticated:
            items = Item.query.filter_by(user_id=current_user.id).all()
            soon = [i for i in items if i.status == "expiring_soon"]
            expired = [i for i in items if i.status == "expired"]

            parts = []
            if soon:
                s_list = ", ".join(f"{i.name} ({i.days_left} days)" for i in soon)
                parts.append(f"Expiring soon: {s_list}.")
            if expired:
                e_list = ", ".join(i.name for i in expired)
                parts.append(f"Already expired: {e_list}.")

            if parts:
                return "Here's your expiry summary: " + " ".join(parts)

        return "If you add your items in the tracker, I can tell you what is expiring soon 💡."

    # recipe ideas from expiring soon items
    if "recipe" in text or "what can i make" in text or "use this" in text:
        if current_user.is_authenticated:
            items = Item.query.filter_by(user_id=current_user.id).all()
            soon = [i for i in items if i.status == "expiring_soon"]
            if soon:
                lines = ["Here are some ideas using items that are expiring soon:"]
                for i in soon:
                    recs = suggest_recipes_for_item(i)
                    if recs:
                        lines.append(f"- {i.name}: " + "; ".join(recs[:2]))
                return " ".join(lines)
        return "Tell me the ingredient name, like 'recipe ideas for milk' and I’ll suggest something 💡."

    # ingredient-specific quick tips
    if "milk" in text:
        return "Milk close to expiry? Make paneer, kheer, curd, or use it in pancakes / milkshakes so it doesn’t go to waste 🥛."
    if "bread" in text:
        return "Old bread ideas: toast with garlic butter, make bread upma, or crispy bread crumbs for cutlets 🍞."
    if "banana" in text:
        return "Spotty bananas are perfect for smoothies, banana bread, or freezing for later milkshakes 🍌."

    # default
    return (
        "I'm a simple offline chatbot living inside your app 😄. "
        "Try asking about expiry, how to store something, or recipe ideas for a particular item."
    )


# ---------------- AUTH ROUTES ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        password = request.form["password"]
        confirm = request.form["confirm"]

        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        existing_user = User.query.filter(
            (User.username == username) | (User.email == email)
        ).first()
        if existing_user:
            flash("Username or email already taken.", "error")
            return redirect(url_for("register"))

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Logged in successfully!", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))
        else:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------------- MAIN APP ROUTES ----------------

@app.route("/")
@login_required
def index():
    # Tabs: all | soon | expired
    filter_mode = request.args.get("filter", "all")

    all_items = Item.query.filter_by(user_id=current_user.id).all()

    if filter_mode == "soon":
        filtered = [i for i in all_items if i.status == "expiring_soon"]
    elif filter_mode == "expired":
        filtered = [i for i in all_items if i.status == "expired"]
    else:
        filter_mode = "all"  # sanitize
        filtered = all_items

    # Sort by days left (smallest first)
    items = sorted(filtered, key=lambda i: i.days_left)

    # Alerts based on ALL items
    alerts = [item for item in all_items if item.status in ("expired", "expiring_soon")]

    # Recipe suggestions only for expiring soon items in current view
    recipe_suggestions = {
        item.id: suggest_recipes_for_item(item)
        for item in items
        if item.status == "expiring_soon"
    }

    return render_template(
        "index.html",
        items=items,
        alerts=alerts,
        today=date.today(),
        current_filter=filter_mode,
        recipe_suggestions=recipe_suggestions,
    )


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_item():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        purchase_date_str = request.form.get("purchase_date", "")
        expiry_date_str = request.form.get("expiry_date", "")
        quantity = request.form.get("quantity", "")
        notes = request.form.get("notes", "")
        file = request.files.get("expiry_image")

        # --- 1. Required: name + purchase date ---
        if not name or not purchase_date_str:
            flash("Item name and purchase date are required.", "error")
            return render_template(
                "form.html",
                action="Add",
                item=None,
                prefill_name=name,
                prefill_purchase=purchase_date_str,
                prefill_expiry=expiry_date_str,
            )

        try:
            purchase_date = date.fromisoformat(purchase_date_str)
        except ValueError:
            flash("Please enter a valid purchase date.", "error")
            return render_template(
                "form.html",
                action="Add",
                item=None,
                prefill_name=name,
                prefill_purchase=purchase_date_str,
                prefill_expiry=expiry_date_str,
            )

        # --- 2. Expiry date: manual OR image ---
        expiry_date = None

        # Option A: manual date has priority
        if expiry_date_str:
            try:
                expiry_date = date.fromisoformat(expiry_date_str)
            except ValueError:
                flash("Please enter a valid expiry date.", "error")
                return render_template(
                    "form.html",
                    action="Add",
                    item=None,
                    prefill_name=name,
                    prefill_purchase=purchase_date_str,
                    prefill_expiry=expiry_date_str,
                )
        else:
            # Option B: try image OCR
            if file and file.filename:
                dt = extract_expiry_from_image(file)
                if not dt:
                    flash(
                        "Could not read a valid expiry date from the image. "
                        "Please type it manually or try another image.",
                        "error",
                    )
                    return render_template(
                        "form.html",
                        action="Add",
                        item=None,
                        prefill_name=name,
                        prefill_purchase=purchase_date_str,
                        prefill_expiry=expiry_date_str,
                    )
                expiry_date = dt
                flash(
                    f"Detected expiry date from image: {dt.strftime('%d-%m-%Y')}.",
                    "success",
                )
            else:
                flash(
                    "Please either enter an expiry date OR upload an image with the expiry details.",
                    "error",
                )
                return render_template(
                    "form.html",
                    action="Add",
                    item=None,
                    prefill_name=name,
                    prefill_purchase=purchase_date_str,
                    prefill_expiry=expiry_date_str,
                )

        # --- 3. Save item ---
        category = guess_category(name)

        new_item = Item(
            name=name,
            category=category,
            purchase_date=purchase_date,
            expiry_date=expiry_date,
            quantity=quantity,
            notes=notes,
            user_id=current_user.id,
        )
        db.session.add(new_item)
        db.session.commit()

        flash("Item added successfully!", "success")
        return redirect(url_for("index"))

    return render_template("form.html", action="Add", item=None)


@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)

    if item.user_id != current_user.id:
        flash("You are not allowed to edit this item.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        item.name = request.form["name"]
        purchase_date_str = request.form["purchase_date"]
        expiry_date_str = request.form["expiry_date"]
        item.quantity = request.form["quantity"]
        item.notes = request.form["notes"]

        if not item.name or not purchase_date_str or not expiry_date_str:
            flash("Name, purchase date, and expiry date are required.", "error")
            return redirect(url_for("edit_item", item_id=item.id))

        try:
            item.purchase_date = date.fromisoformat(purchase_date_str)
            item.expiry_date = date.fromisoformat(expiry_date_str)
        except ValueError:
            flash("Please enter valid dates.", "error")
            return redirect(url_for("edit_item", item_id=item.id))

        item.category = guess_category(item.name)

        db.session.commit()
        flash("Item updated successfully!", "success")
        return redirect(url_for("index"))

    return render_template("form.html", action="Edit", item=item)


@app.route("/delete/<int:item_id>", methods=["POST"])
@login_required
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)

    if item.user_id != current_user.id:
        flash("You are not allowed to delete this item.", "error")
        return redirect(url_for("index"))

    db.session.delete(item)
    db.session.commit()
    flash("Item deleted successfully!", "success")
    return redirect(url_for("index"))


# ---------------- CHAT ROUTE ----------------

@app.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    user_message = None
    bot_reply = None

    if request.method == "POST":
        user_message = request.form.get("message", "").strip()
        if user_message:
            bot_reply = ask_chatbot(user_message)
        else:
            flash("Please type a message for the chatbot.", "error")

    return render_template("chat.html", user_message=user_message, bot_reply=bot_reply)


# ---------------- MAIN ----------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
