from flask import Flask, render_template, request, jsonify, send_file, session
import os
import sqlite3
import pandas as pd
from io import BytesIO
from requests.auth import HTTPBasicAuth
from urllib.parse import urljoin
import requests
from plotly.subplots import make_subplots
import plotly.graph_objects as go
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 配置信息
BASE_URL = os.getenv("BASE_URL", "https://dav.jianguoyun.com/dav/")
USERNAME = os.getenv("USERNAME", "User")
PASSWORD = os.getenv("PASSWORD", "Pwd")
AUTH = HTTPBasicAuth(USERNAME, PASSWORD)
RENDER_PASSWORD = os.getenv("USER_PASSWORD")

if not RENDER_PASSWORD:
    raise ValueError("未设置 USER_PASSWORD 环境变量，请在 Render 中添加密码。")

# Flask 应用初始化
app = Flask(__name__)
app.secret_key = os.urandom(24)

# 项目目录和文件夹设置
PROJECT_ROOT = "/tmp"
OUTPUT_FOLDER = os.path.join(PROJECT_ROOT, "output_charts")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
DATABASE_FILE = os.path.join(PROJECT_ROOT, "data.db")
EXCEL_FILE = "产品净值数据/WeeklyReport_各项指标.xlsx"

# 数据库初始化
def initialize_database():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 下载 Excel 文件
        url = urljoin(BASE_URL, EXCEL_FILE)
        response = requests.get(url, auth=AUTH)
        if response.status_code == 200:
            with open(os.path.join(PROJECT_ROOT, "WeeklyReport_各项指标.xlsx"), "wb") as file:
                file.write(response.content)
            logging.info("Excel 文件下载成功")

        # 加载数据到 SQLite 数据库
        df = pd.read_excel(os.path.join(PROJECT_ROOT, "WeeklyReport_各项指标.xlsx"))
        cursor.execute("DROP TABLE IF EXISTS products")
        df.to_sql("products", conn, index=False, if_exists="replace")
        logging.info("数据已加载到 SQLite 数据库")

        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"初始化数据库时出现错误：{e}")

# 初始化数据库
initialize_database()

@app.route("/")
def index():
    # 返回静态 HTML 页面，让前端处理登录逻辑
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    password = request.form.get("password")
    if password == RENDER_PASSWORD:
        session["logged_in"] = True
        logging.info("用户登录成功")
        return jsonify({"success": True}), 200
    logging.warning("用户登录失败")
    return jsonify({"error": "密码错误"}), 401

# 在受保护路由中验证登录状态
@app.before_request
def require_login():
    open_routes = ["/", "/login", "static"]
    if request.endpoint not in open_routes and not session.get("logged_in"):
        if request.endpoint == "get_table_data" or request.endpoint == "get_strategies":
            # 返回空数据而非错误
            return jsonify({"columns": [], "data": []}), 200
        return jsonify({"error": "未登录"}), 401

@app.route("/filter", methods=["POST"])
def filter_data():
    try:
        strategy = request.form.get("strategy")
        if not strategy:
            return jsonify({"error": "无效的策略"}), 400

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        query = """
            SELECT * FROM products
            WHERE 产品策略 = ?
            ORDER BY 年化收益率 DESC, 本周收益率 DESC
        """
        cursor.execute(query, (strategy,))
        data = cursor.fetchall()

        cursor.execute("PRAGMA table_info(products)")
        columns = [row[1] for row in cursor.fetchall()]

        conn.close()
        return jsonify({"columns": columns, "data": data})
    except Exception as e:
        logging.error(f"筛选数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/strategies")
def get_strategies():
    if not session.get("logged_in"):
        return jsonify({"error": "未登录"}), 401

    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT 产品策略 FROM products")
        strategies = [row[0] for row in cursor.fetchall()]
        conn.close()
        return jsonify({"strategies": strategies})
    except Exception as e:
        logging.error(f"获取策略列表时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/table_data")
def get_table_data():
    if not session.get("logged_in"):
        return jsonify({"error": "未登录"}), 401

    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(products)")
        columns = [row[1] for row in cursor.fetchall()]

        cursor.execute("""
            SELECT * FROM products
            ORDER BY 产品策略 ASC, 年化收益率 DESC, 本周收益率 DESC
        """)
        data = cursor.fetchall()

        conn.close()
        return jsonify({"columns": columns, "data": data})
    except Exception as e:
        logging.error(f"获取表格数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500


@app.route("/search", methods=["POST"])
def search_data():
    try:
        keywords = request.form.get("keywords", "").strip()
        if not keywords:
            return jsonify({"error": "未提供关键词"}), 400

        keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        query = f"""
            SELECT * FROM products
            WHERE 产品代码 IN ({','.join(['?'] * len(keyword_list))})
            OR 产品名称 LIKE ?
            ORDER BY 年化收益率 DESC, 本周收益率 DESC
        """
        cursor.execute(query, keyword_list + [f"%{keywords}%"])
        data = cursor.fetchall()

        cursor.execute("PRAGMA table_info(products)")
        columns = [row[1] for row in cursor.fetchall()]

        conn.close()
        return jsonify({"columns": columns, "data": data})
    except Exception as e:
        logging.error(f"搜索数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/add_chart", methods=["POST"])
def add_chart():
    try:
        product_codes = request.form.getlist("product_codes[]")
        if not product_codes:
            return jsonify({"error": "无效的产品代码"}), 400

        chart_path = create_subplots(product_codes)
        if not chart_path:
            logging.warning("合并图表生成失败")
            return jsonify({"error": "生成图表失败"}), 500

        logging.info("合并图表生成成功")
        return jsonify({"success": True, "chart_path": chart_path})
    except Exception as e:
        logging.error(f"生成合并图表时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/download_chart/<product_code>")
def download_chart(product_code):
    try:
        chart_url = urljoin(BASE_URL, f"{OUTPUT_FOLDER}/{product_code}_chart.html")
        response = requests.get(chart_url, auth=AUTH)
        if response.status_code == 200:
            local_path = os.path.join(OUTPUT_FOLDER, f"{product_code}_chart.html")
            with open(local_path, "wb") as file:
                file.write(response.content)
            return send_file(local_path, as_attachment=True, download_name=f"{product_code}_chart.html")
        return jsonify({"error": "图表未找到"}), 404
    except Exception as e:
        logging.error(f"下载图表时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/output_charts/<path:filename>")
def serve_temp_file(filename):
    try:
        file_path = os.path.join(OUTPUT_FOLDER, filename)
        return send_file(file_path)
    except Exception as e:
        logging.error(f"提供文件 {filename} 时出错：{e}")
        return jsonify({"error": "文件不存在或已被删除"}), 404

@app.route("/delete_row", methods=["POST"])
def delete_row():
    try:
        product_code = request.form.get("product_code")
        if not product_code:
            return jsonify({"error": "未提供产品代码"}), 400

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM products WHERE 产品代码 = ?", (product_code,))
        conn.commit()
        conn.close()

        logging.info(f"删除产品成功：{product_code}")
        return jsonify({"success": True, "product_code": product_code})
    except Exception as e:
        logging.error(f"删除产品时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

def create_subplots(product_codes):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT 产品代码, 产品名称 FROM products")
        product_mapping = dict(cursor.fetchall())

        conn.close()

        all_dates = []
        product_data = {}

        for product_code in product_codes:
            json_url = urljoin(BASE_URL, f"产品净值数据/output_charts/{product_code}_chart.json")
            response = requests.get(json_url, auth=AUTH)
            if response.status_code == 200:
                try:
                    plotly_data = response.json()
                    if "data" in plotly_data:
                        for trace in plotly_data["data"]:
                            x_dates = pd.to_datetime(trace["x"])
                            y_values = trace["y"]
                            all_dates.extend(x_dates)
                            product_data[product_code] = {"x": x_dates, "y": y_values}
                except Exception:
                    continue

        if not all_dates:
            logging.warning("无有效数据用于生成图表")
            return None

        rows, cols = len(product_codes), 1
        fig = make_subplots(rows=rows, cols=cols)

        for idx, product_code in enumerate(product_codes):
            if product_code in product_data:
                x_data = product_data[product_code]["x"]
                y_data = product_data[product_code]["y"]
                fig.add_trace(go.Scatter(x=x_data, y=y_data, name=product_code), row=idx + 1, col=1)

        chart_path = os.path.join(OUTPUT_FOLDER, "merged_chart.html")
        fig.write_html(chart_path)
        logging.info(f"合并图表保存到：{chart_path}")
        return chart_path
    except Exception as e:
        logging.error(f"生成合并图表时出现错误：{e}")
        return None

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
