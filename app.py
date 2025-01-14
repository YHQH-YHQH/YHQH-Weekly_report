from flask import Flask, render_template, request, jsonify, send_file, session
import os
import time
import sqlite3
import pandas as pd
from io import BytesIO
from requests.auth import HTTPBasicAuth
from urllib.parse import urljoin
import requests
from plotly.subplots import make_subplots
import plotly.graph_objects as go
import logging
import shutil  # 需要在has_sufficient_tmp_space里使用

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 配置信息
BASE_URL = os.getenv("BASE_URL", "https://dav.jianguoyun.com/dav/")
USERNAME = os.getenv("USERNAME", "User")
PASSWORD = os.getenv("PASSWORD", "Pwd")
AUTH = HTTPBasicAuth(USERNAME, PASSWORD)
RENDER_PASSWORD = os.getenv("USER_PASSWORD")
MAX_TMP_SPACE_MB = 500

if not RENDER_PASSWORD:
    raise ValueError("未设置 USER_PASSWORD 环境变量，请在 Render 中添加密码。")

# Flask 应用初始化
app = Flask(__name__)

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
        # --- 改动1：指定新增列“同策略表现”和“近8周排名”读取为字符串类型 ---
        # 其余列按默认推断，或可根据需求自行指定
        dtype_map = {
            "同策略表现": str,
            "近8周排名": str
        }
        df = pd.read_excel(os.path.join(PROJECT_ROOT, "WeeklyReport_各项指标.xlsx"),
                           dtype=dtype_map)
        cursor.execute("DROP TABLE IF EXISTS products")
        # 如果希望在数据库中强制使用TEXT类型，可使用类似：
        # df.to_sql("products", conn, index=False, if_exists="replace", dtype={"同策略表现": "TEXT", "近8周排名": "TEXT"})
        # 这里演示只使用默认映射也可以
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
    return render_template("index.html")

@app.route('/favicon.ico')
def favicon():
    return '', 204  # 返回一个空响应，避免 404 错误

@app.route("/filter", methods=["POST"])
def filter_data():
    password = request.form.get("password")
    if password != RENDER_PASSWORD:
        return jsonify({"error": "密码错误"}), 403

    try:
        strategy = request.form.get("strategy")

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        # 判断是否需要筛选策略
        if strategy:
            query = """
                SELECT * FROM products
                WHERE 产品策略 = ?
                ORDER BY 年化收益率 DESC, 本周收益率 DESC
            """
            cursor.execute(query, (strategy,))
        else:
            query = """
                SELECT * FROM products
                ORDER BY 产品策略 ASC, 年化收益率 DESC, 本周收益率 DESC
            """
            cursor.execute(query)

        data = cursor.fetchall()

        cursor.execute("PRAGMA table_info(products)")
        columns = [row[1] for row in cursor.fetchall()]

        conn.close()
        return jsonify({"columns": columns,
                        "data": [dict(zip(columns, row)) for row in data]})
    except Exception as e:
        logging.error(f"筛选数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500


@app.route("/strategies", methods=["POST"])
def get_strategies():
    try:
        # 获取前端传递的密码
        password = request.form.get("password")
        if password != RENDER_PASSWORD:  # 验证密码
            return jsonify({"error": "密码错误"}), 403

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT 产品策略 FROM products")
        strategies = [row[0] for row in cursor.fetchall()]
        conn.close()
        return jsonify({"strategies": strategies})
    except Exception as e:
        logging.error(f"获取策略列表时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500


@app.route("/table_data", methods=["POST"])
def get_table_data():
    try:
        # 获取前端传递的密码
        password = request.form.get("password")
        if password != RENDER_PASSWORD:  # 验证密码
            return jsonify({"error": "密码错误"}), 403

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
        return jsonify({
            "columns": columns,
            "data": [dict(zip(columns, row)) for row in data]
        })
    except Exception as e:
        logging.error(f"获取表格数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500


@app.route("/search", methods=["POST"])
def search_data():
    password = request.form.get("password")
    if password != RENDER_PASSWORD:
        return jsonify({"error": "密码错误"}), 403
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
        return jsonify({"columns": columns,
                        "data": [dict(zip(columns, row)) for row in data]})
    except Exception as e:
        logging.error(f"搜索数据时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/add_chart", methods=["POST"])
def add_chart():
    password = request.form.get("password")
    if password != RENDER_PASSWORD:
        return jsonify({"error": "密码错误"}), 403

    try:
        product_codes = request.form.getlist("product_codes[]")
        if not product_codes:
            return jsonify({"error": "无效的产品代码"}), 400

        # 动态生成文件名
        if len(product_codes) > 3:
            chart_name = f"{'_'.join(product_codes[:3])}_等_合并.html"
        else:
            chart_name = f"{'_'.join(product_codes)}_合并.html"

        # 如果只选择了一个产品，就直接返回单图
        if len(product_codes) == 1:
            single_chart_url = urljoin(BASE_URL, f"产品净值数据/output_charts/{product_codes[0]}_chart.html")
            response = requests.get(single_chart_url, auth=AUTH)
            if response.status_code == 200:
                local_path = os.path.join(PROJECT_ROOT, f"{product_codes[0]}_chart.html")
                with open(local_path, "wb") as file:
                    file.write(response.content)
                return send_file(local_path, as_attachment=True, download_name=f"{product_codes[0]}_chart.html")
            else:
                return jsonify({"error": f"图表 {product_codes[0]} 未找到"}), 404

        chart_path = os.path.join(OUTPUT_FOLDER, chart_name)
        # 如果对应的合并图已存在，直接返回
        if os.path.exists(chart_path):
            return send_file(chart_path, as_attachment=True, download_name=chart_name)

        # 如果不存在，尝试生成合并图表
        chart_path = create_subplots(product_codes, chart_name)
        if not chart_path:
            logging.warning("合并图表生成失败，可能是空间不足")
            if not has_sufficient_tmp_space():
                logging.info("清空 /tmp 文件夹，释放空间")
                clear_tmp_folder()
                chart_path = create_subplots(product_codes, chart_name)
                if not chart_path:
                    return jsonify({"error": "生成合并图表失败"}), 500
            else:
                return jsonify({"error": "生成合并图表失败"}), 500

        return send_file(chart_path, as_attachment=True, download_name=chart_name)

    except Exception as e:
        logging.error(f"生成合并图表时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500

@app.route("/download_chart/<product_code>", methods=["POST"])
def download_chart(product_code):
    password = request.form.get("password")
    if password != RENDER_PASSWORD:
        return jsonify({"error": "密码错误"}), 403
    try:
        chart_url = urljoin(BASE_URL, f"产品净值数据/output_charts/{product_code}_chart.html")
        logging.info(f"Fetching chart from: {chart_url}")
        response = requests.get(chart_url, auth=AUTH)
        logging.info(f"WebDAV Response: {response.status_code}")
        if response.status_code == 200:
            local_path = os.path.join(OUTPUT_FOLDER, f"{product_code}_chart.html")
            logging.info(f"Saving chart to local path: {local_path}")
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

        if cursor.rowcount == 0:
            return jsonify({"error": "未找到指定的产品"}), 404

        conn.close()

        logging.info(f"删除产品成功：{product_code}")
        return jsonify({"success": True, "product_code": product_code})
    except Exception as e:
        logging.error(f"删除产品时出现错误：{e}")
        return jsonify({"error": "服务器错误"}), 500


def create_subplots(product_codes, chart_name):
    try:
        # 1. 从 SQLite 数据库中获取 [产品代码 -> 产品名称] 映射
        conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "data.db"))
        cursor = conn.cursor()
        cursor.execute("SELECT 产品代码, 产品名称 FROM products")
        product_mapping = dict(cursor.fetchall())
        conn.close()

        # 2. 准备一个字典，存储每个产品的所有 Trace
        #    product_data[product_code] = [list_of_trace_dict]
        product_data = {}

        # 3. 循环读取各产品对应的JSON文件，保留“最大回撤区域”“净值曲线”等所有 Trace
        for product_code in product_codes:
            json_url = urljoin(BASE_URL, f"产品净值数据/output_charts/{product_code}_chart.json")
            response = requests.get(json_url, auth=AUTH)
            if response.status_code == 200:
                try:
                    plotly_data = response.json()
                    if "data" in plotly_data:
                        for trace in plotly_data["data"]:
                            x_dates = pd.to_datetime(trace.get("x", []))
                            y_values = trace.get("y", [])
                            if len(x_dates) > 0 and len(y_values) > 0:
                                # 初始化列表
                                if product_code not in product_data:
                                    product_data[product_code] = []

                                # 将该 trace 的核心信息保存下来
                                # 注意：我们把 "mode" "fill" "fillcolor" "hovertemplate" 等都保留
                                # 并检查 yaxis 是否是 "y2"
                                trace_dict = {
                                    "x": x_dates,
                                    "y": y_values,
                                    "mode": trace.get("mode", "lines"),
                                    "fill": trace.get("fill", None),
                                    "fillcolor": trace.get("fillcolor", None),
                                    "name": trace.get("name", product_code),
                                    "hovertemplate": trace.get("hovertemplate", None),
                                    "yaxis": trace.get("yaxis", "y")  # 如果是 "y2" 就用副轴
                                }
                                product_data[product_code].append(trace_dict)

                except Exception as e:
                    logging.warning(f"解析 {product_code} JSON 数据时发生错误: {e}")
                    continue

        # 4. 若所有产品都没有有效 Trace，返回 None
        valid_products = [pc for pc in product_data if product_data[pc]]
        if not valid_products:
            logging.warning("没有有效数据可用于生成图表")
            return None

        # 5. 自动行列布局
        n = len(product_codes)
        rows = int((n - 1) ** 0.5) + 1  # 行数
        cols = rows  # 列数

        # 6. 每个 subplot 标题： '产品代码 + 产品名称'
        subplot_titles = []
        for code in product_codes:
            product_name = product_mapping.get(code, "")
            subplot_titles.append(f"{code} {product_name}")

        # 7. 构造 specs，使每个子图都支持 secondary_y
        specs = [[{"secondary_y": True} for _ in range(cols)] for _ in range(rows)]

        # 8. 创建多子图，并加上标题
        fig = make_subplots(
            rows=rows,
            cols=cols,
            subplot_titles=subplot_titles,
            specs=specs
        )

        # 9. 在多子图上逐一添加 Trace
        for i, product_code in enumerate(product_codes):
            row = i // cols + 1
            col = i % cols + 1
            # 避免空数据的情况
            if product_code not in product_data or not product_data[product_code]:
                continue

            # 当前产品的多个 Trace
            for trace_info in product_data[product_code]:
                secondary_y = (trace_info["yaxis"] == "y2")  # 判断是否走副轴
                scatter_obj = go.Scatter(
                    x=trace_info["x"],
                    y=trace_info["y"],
                    mode=trace_info["mode"],
                    name=trace_info["name"],
                    fill=trace_info["fill"],
                    fillcolor=trace_info["fillcolor"],
                    hovertemplate=trace_info["hovertemplate"]
                )
                fig.add_trace(scatter_obj, row=row, col=col, secondary_y=secondary_y)

                # 如果是 y2，就让其 autorange = "reversed"
                if secondary_y:
                    fig.update_yaxes(
                        autorange="reversed",
                        row=row, col=col,
                        secondary_y=True
                    )

        # 10. 布局调优，图例、大小、标题等
        fig.update_layout(
            height=rows * 400,
            width=cols * 500,
            title_text="跨产品图表比较",
            showlegend=True
        )

        # 11. 写入 HTML 并返回路径
        chart_path = os.path.join(OUTPUT_FOLDER, chart_name)
        fig.write_html(chart_path)
        logging.info(f"合并图表保存到：{chart_path}")
        return chart_path

    except Exception as e:
        logging.error(f"生成合并图表时出现错误：{e}")
        return None


def has_sufficient_tmp_space():
    total, used, free = shutil.disk_usage(PROJECT_ROOT)
    free_mb = free // (1024 * 1024)
    return free_mb >= MAX_TMP_SPACE_MB * 0.1  # 剩余至少 10% 才认为足够

def clear_tmp_folder():
    for filename in os.listdir(PROJECT_ROOT):
        filepath = os.path.join(PROJECT_ROOT, filename)
        try:
            if os.path.isfile(filepath):
                os.unlink(filepath)
        except Exception as e:
            logging.error(f"清空 /tmp 文件夹时删除文件 {filename} 失败：{e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
