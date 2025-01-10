from flask import Flask, render_template, request, jsonify, Response, send_file, make_response
import os
import sqlite3
import pandas as pd
import json
from requests.auth import HTTPBasicAuth
from urllib.parse import urljoin
from io import BytesIO
import requests
from plotly.subplots import make_subplots
import plotly.graph_objects as go

# 从环境变量中获取坚果云 WebDAV 配置信息和密码
BASE_URL = os.getenv("BASE_URL", "https://dav.jianguoyun.com/dav/")
USERNAME = os.getenv("USERNAME", "User")
PASSWORD = os.getenv("PASSWORD", "Pwd")
AUTH = HTTPBasicAuth(USERNAME, PASSWORD)
RENDER_PASSWORD = os.getenv("USER_PASSWORD")

if not RENDER_PASSWORD:
    raise ValueError("未设置 USER_PASSWORD 环境变量，请在 Render 中添加一个长而复杂的密码")

# 项目目录和文件夹设置
PROJECT_ROOT = "/tmp"  # 使用 Render 的临时可写目录
OUTPUT_FOLDER = os.path.join(PROJECT_ROOT, "output_charts")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
input_folder = "产品净值数据/output_charts"
result_file = "产品净值数据/WeeklyReport_各项指标.xlsx"

# 初始化 Flask 应用
app = Flask(__name__)

# 将 Excel 数据加载到 SQLite 数据库
def initialize_database():
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
    cursor = conn.cursor()
    
    # 下载 Excel 文件并加载到数据库
    url = urljoin(BASE_URL, result_file)
    response = requests.get(url, auth=AUTH, stream=True)
    if response.status_code == 200:
        with open(os.path.join(PROJECT_ROOT, "WeeklyReport_各项指标.xlsx"), "wb") as file:
            file.write(response.content)
        print("Excel 文件下载成功")

    # 读取 Excel 数据并加载到数据库
    df = pd.read_excel(os.path.join(PROJECT_ROOT, "WeeklyReport_各项指标.xlsx"))
    cursor.execute("DROP TABLE IF EXISTS products")
    df.to_sql("products", conn, index=False, if_exists="replace")
    print("数据已加载到 SQLite 数据库")

    conn.commit()
    conn.close()

# 初始化数据库
initialize_database()

# 全局请求拦截器：验证密码，但跳过根路径 "/"
@app.before_request
def require_password():
    # 跳过根路径和静态资源的密码验证
    if request.path in ["/", "/static"] or request.path.startswith("/static/"):
        return

    # 从请求中获取用户输入的密码
    user_password = request.args.get("password") or request.form.get("password")
    if not user_password:
        return jsonify({"error": "需要密码访问，请在请求中提供密码"}), 401

    if user_password != RENDER_PASSWORD:
        return jsonify({"error": "密码错误，访问被拒绝"}), 403

@app.route("/")
def index():
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
    cursor = conn.cursor()

    # 获取产品策略
    cursor.execute("SELECT DISTINCT 产品策略 FROM products")
    strategies = [row[0] for row in cursor.fetchall()]

    # 获取列名
    cursor.execute("PRAGMA table_info(products)")
    columns = [row[1] for row in cursor.fetchall()]

    # 获取数据并按指定顺序排序
    query = """
        SELECT * FROM products
        ORDER BY 产品策略 ASC, 年化收益率 DESC, 本周收益率 DESC
    """
    cursor.execute(query)
    data = [dict(zip(columns, row)) for row in cursor.fetchall()]

    conn.close()

    # 渲染模板
    rendered_html = render_template("index.html", strategies=strategies, columns=columns, data=data)
    response = make_response(rendered_html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response



@app.route("/filter", methods=["POST"])
def filter_data():
    selected_strategy = request.form.get("strategy")
    if not selected_strategy:
        return jsonify({"error": "No strategy provided"}), 400

    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
    cursor = conn.cursor()

    # SQL 筛选数据
    query = "SELECT * FROM products WHERE 产品策略 = ?"
    cursor.execute(query, (selected_strategy,))
    result = cursor.fetchall()

    conn.close()

    if not result:
        return jsonify([])

    # 转换为字典格式
    columns = [desc[0] for desc in cursor.description]
    result_dict = [dict(zip(columns, row)) for row in result]
    return jsonify(result_dict)

@app.route("/search", methods=["POST"])
def search_data():
    keywords = request.form.get("keywords", "")
    if not keywords:
        return jsonify({"error": "No keywords provided"}), 400

    keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    if not keyword_list:
        return jsonify([])

    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
    cursor = conn.cursor()

    # 搜索逻辑
    query = """
        SELECT * FROM products
        WHERE 产品代码 IN ({})
        OR 产品名称 LIKE ?
    """.format(",".join(["?"] * len(keyword_list)))
    params = keyword_list + ["%" + kw + "%" for kw in keyword_list]
    cursor.execute(query, params)
    result = cursor.fetchall()

    conn.close()

    if not result:
        return jsonify([])

    columns = [desc[0] for desc in cursor.description]
    result_dict = [dict(zip(columns, row)) for row in result]
    return jsonify(result_dict)

@app.route("/add_chart", methods=["POST"])
def add_chart():
    try:
        product_codes = request.form.getlist("product_codes[]")
        if not product_codes:
            return jsonify({"error": "未提供产品代码"}), 400

        chart_path = create_subplots(product_codes)
        if not chart_path:
            return jsonify({"error": "合并图表生成失败"}), 500

        print(f"合并图表已成功生成：{chart_path}")
        return jsonify({"success": True, "chart_path": f"/output_charts/{os.path.basename(chart_path)}"})

    except Exception as e:
        print(f"生成合并图表时出现异常: {e}")
        return jsonify({"error": "服务器错误"}), 500

    
@app.route("/download_chart/<product_code>")
def download_chart(product_code):
    try:
        chart_url = urljoin(BASE_URL, f"{input_folder}/{product_code}_chart.html")
        response = requests.get(chart_url, auth=AUTH)
        if response.status_code == 200:
            local_path = os.path.join(OUTPUT_FOLDER, f"{product_code}_chart.html")
            with open(local_path, "wb") as file:
                file.write(response.content)
            return send_file(local_path, as_attachment=True, download_name=f"{product_code}_chart.html")
        else:
            return jsonify({"error": "Chart not found"}), 404
    except Exception as e:
        print(f"下载图表时出错: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route("/output_charts/<path:filename>")
def serve_temp_file(filename):
    file_path = os.path.join(OUTPUT_FOLDER, filename)
    try:
        response = send_file(file_path)
        @response.call_on_close
        def remove_temp_file():
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"删除临时文件失败: {e}")
        return response
    except Exception as e:
        print(f"无法提供文件 {filename}: {e}")
        return "文件不存在或已被删除", 404

@app.route("/delete_row", methods=["POST"])
def delete_row():
    product_code = request.form.get("product_code")
    if not product_code:
        return jsonify({"error": "No product code provided"}), 400

    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
    cursor = conn.cursor()

    # 删除对应的行
    cursor.execute("DELETE FROM products WHERE 产品代码 = ?", (product_code,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "product_code": product_code})

def create_subplots(product_codes):
    global OUTPUT_FOLDER
    try:
        all_dates = []
        product_data = {}

        # 连接数据库获取产品代码和名称映射
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
        cursor = conn.cursor()
        cursor.execute("SELECT 产品代码, 产品名称 FROM products")
        product_mapping = dict(cursor.fetchall())
        conn.close()

        # 加载每个产品的图表数据
        for product_code in product_codes:
            json_url = urljoin(BASE_URL, f"{input_folder}/{product_code}_chart.json")
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
                except json.JSONDecodeError as e:
                    print(f"警告：无法解析 JSON 文件 {json_url}: {e}")
            else:
                print(f"警告：未找到图表数据文件 {json_url}")

        if not all_dates:
            print("错误：没有有效数据用于生成图表。")
            return None

        # 日期范围补全
        min_date, max_date = min(all_dates), max(all_dates)
        full_x_range = pd.date_range(start=min_date, end=max_date, freq="D")

        # 动态布局：计算子图行和列
        n = len(product_codes)
        rows = int((n - 1) ** 0.5) + 1
        cols = rows
        fig = make_subplots(rows=rows, cols=cols, subplot_titles=[
            f"{product_mapping.get(code, '未知产品')} ({code})" for code in product_codes
        ])

        # 填充子图数据
        for i, product_code in enumerate(product_codes):
            row = i // cols + 1
            col = i % cols + 1
            if product_code in product_data:
                x_data = product_data[product_code]["x"]
                y_data = product_data[product_code]["y"]

                # 补齐日期范围
                full_y_data = [None] * len(full_x_range)
                x_to_index = {date: idx for idx, date in enumerate(full_x_range)}
                for x, y in zip(x_data, y_data):
                    if x in x_to_index:
                        full_y_data[x_to_index[x]] = y

                # 添加到子图
                fig.add_trace(
                    go.Scatter(x=full_x_range, y=full_y_data, name=product_code),
                    row=row, col=col
                )

        # 保存图表到 OUTPUT_FOLDER
        chart_path = os.path.join(OUTPUT_FOLDER, f"merged_chart.html")
        fig.write_html(chart_path)
        print(f"图表已保存到: {chart_path}")
        return chart_path

    except Exception as e:
        print(f"生成子图时出现异常: {e}")
        return None


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
