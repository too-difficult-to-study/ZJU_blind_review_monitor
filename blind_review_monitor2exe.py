"""
ZJU盲审结果监控工具
基于以下开源项目修改：
https://gist.github.com/FanBB2333/229d177bbffdb1adc96f5f8a65a3c47f

修改内容：
- 适配windows端生成可执行文件
- 新增钉钉通知
- 支持无头浏览器
"""

import os
import sys
import io
import time
import json
import pickle
import hashlib
import requests
from datetime import datetime
from pathlib import Path

# ========== 全局编码修复 强制UTF8 ==========
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

print("正在启动程序...", flush=True)
sys.stdout.flush()

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import *

# ==================== 配置 ====================
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=d2e2cb4eb12c3eedb175be3cc516e0755445080e0f8b0720eb336e86f01ef84c"

# 手动输入账号密码 输入完按回车
print("="*40)
os.environ["ZJUAM_ACCOUNT"] = input("请输入浙大统一账号：")
os.environ["ZJUAM_PASSWORD"] = input("请输入统一密码：")
print("="*40)

TARGET_URL = "https://yjsy.zju.edu.cn/dashboard/workplace?dm=xw_sqzt&mode=2&role=1&back=dashboard"
LOGIN_URL = "https://zjuam.zju.edu.cn/cas/login"
COOKIES_FILE = Path(__file__).parent / "cookies.pkl"
RESULT_CACHE_FILE = Path(__file__).parent / "last_result.json"

REFRESH_INTERVAL = 60
TEST_MODE = False

# ==================== 通知（仅保留钉钉，删掉桌面通知防闪退） ====================
def send_dingtalk_notification(title: str, msg: str):
    if not DINGTALK_WEBHOOK:
        return
    try:
        requests.post(
            DINGTALK_WEBHOOK,
            json={"msgtype": "text", "text": {"content": f"{title}\n{msg}"}},
            timeout=10
        )
        print("[✅ 钉钉通知发送成功]", flush=True)
    except Exception as e:
        print(f"[❌ 钉钉通知失败：{str(e)[:30]}]", flush=True)

def send_notification(title, body):
    send_dingtalk_notification(title, body)

# ==================== 驱动【极简配置，杜绝启动失败】 ====================
def setup_driver():
    try:
        options = webdriver.ChromeOptions()
        # 无头模式 关键参数
        options.add_argument("--headless=new")
        options.add_argument("--no-proxy-server")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("window-size=1920,1080")
        options.add_experimental_option('excludeSwitches', ['enable-logging'])

        driver = webdriver.Chrome(options=options)
        print("✅ 无头Chrome 启动成功（后台运行）")
        return driver
    except Exception as e:
        print(f"❌ 启动无头Chrome失败：{e}")
        return None

def save_cookies(driver):
    try:
        with open(COOKIES_FILE, "wb") as f:
            pickle.dump(driver.get_cookies(), f)
    except:
        pass

def load_cookies(driver):
    if not COOKIES_FILE.exists():
        return False
    try:
        driver.get("https://yjsy.zju.edu.cn")
        time.sleep(1)
        for c in pickle.load(open(COOKIES_FILE, "rb")):
            if "expiry" in c:
                del c["expiry"]
            driver.add_cookie(c)
        return True
    except:
        return False

# ==================== 登录 ====================
def perform_login(driver):
    print("[开始自动登录浙大统一认证...]", flush=True)
    try:
        driver.get(LOGIN_URL)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "username")))
        driver.find_element(By.ID, "username").send_keys(os.environ["ZJUAM_ACCOUNT"])
        driver.find_element(By.ID, "password").send_keys(os.environ["ZJUAM_PASSWORD"])
        time.sleep(1)
        driver.find_element(By.ID, "dl").click()
        time.sleep(5)
        driver.get(TARGET_URL)
        time.sleep(5)
        save_cookies(driver)
        print("✅ 登录成功，进入盲审页面")
        return True
    except Exception as e:
        print(f"❌ 登录失败：{e}")
        return False

# ==================== 登录判断 ====================
def is_logged_in(driver):
    try:
        if "zjuam.zju.edu.cn" in driver.current_url:
            return False
        driver.find_element(By.ID, "username")
        return False
    except NoSuchElementException:
        return True

# ==================== 读取盲审结果 ====================
def extract_review_results(driver):
    reviews = []
    if TEST_MODE:
        return [
            {"overall": "B（良好）", "result": "同意修改后直接答辩"},
            {"overall": "A（优秀）", "result": "同意修改后直接答辩"},
            {"overall": "B（良好）", "result": "同意修改后直接答辩"}
        ]
    try:
        time.sleep(3)
        js_code = """
        const tables = document.querySelectorAll('table');
        let targetTable = null;
        for(let table of tables) {
            if(table.innerText.includes('专家姓名') && table.innerText.includes('总体评价')) {
                targetTable = table;
                break;
            }
        }
        if(!targetTable) return [];
        const rows = targetTable.querySelectorAll('tbody tr');
        const res = [];
        rows.forEach(row=>{
            let tds = row.querySelectorAll('td');
            if(tds.length>=4){
                let ztpj = tds[2].innerText.trim();
                let pyjg = tds[3].innerText.trim();
                let invalid = ['是','否','1','2','3','','0'];
                if(!invalid.includes(ztpj)&&!invalid.includes(pyjg)){
                    res.push({overall:ztpj,result:pyjg});
                }
            }
        });
        return res;
        """
        reviews = driver.execute_script(js_code)
    except:
        pass
    return reviews

# ==================== 变化检测 ====================
def get_result_hash(r):
    return hashlib.md5(json.dumps(r, sort_keys=True).encode()).hexdigest() if r else ""

def load_last_result():
    if not RESULT_CACHE_FILE.exists():
        return None, ""
    try:
        d = json.load(open(RESULT_CACHE_FILE, "r", encoding="utf-8"))
        return d, get_result_hash(d)
    except:
        return None, ""

def save_result(r):
    with open(RESULT_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)

# ==================== 主程序 死循环不闪退 ====================
def main():
    print(f"[{datetime.now()}] 盲审监控启动", flush=True)
    send_notification("盲审监控", "已启动，持续监听盲审结果")

    # 循环初始化驱动，彻底杜绝单次崩溃
    driver = None
    while True:
        try:
            # 驱动不存在就重建
            if driver is None:
                driver = setup_driver()
                if driver is None:
                    print("⚠️ 浏览器启动失败，10秒后重试...")
                    time.sleep(10)
                    continue
                load_cookies(driver)
                driver.get(TARGET_URL)
                time.sleep(3)

            print("\n" + "="*50)
            print(f"[{datetime.now()}] 定时刷新检查")

            if not is_logged_in(driver):
                print("⚠️ 未登录，重新登录")
                perform_login(driver)
            else:
                print("✅ 已保持登录状态")

            if "xw_sqzt" not in driver.current_url:
                driver.get(TARGET_URL)
                time.sleep(4)

            res = extract_review_results(driver)
            print("\n======= 当前盲审结果 =======")
            if res:
                for i, item in enumerate(res, 1):
                    print(f"第{i}位 | {item['overall']} | {item['result']}")
            else:
                print("暂无专家评阅数据")

            old_data, old_hash = load_last_result()
            new_hash = get_result_hash(res)
            if res and new_hash != old_hash:
                msg = "\n".join([f"专家{i}：{x['overall']}，{x['result']}" for i,x in enumerate(res,1)])
                send_notification("🎉 盲审结果更新", msg)
                save_result(res)

            time.sleep(REFRESH_INTERVAL)
            driver.refresh()
            time.sleep(2)

        except Exception as e:
            print(f"\n❌ 本轮异常：{str(e)[:50]}")
            print("⏳ 10秒后自动重试，程序不会退出")
            time.sleep(10)
            continue

if __name__ == "__main__":
    # 最外层死兜底，防止任何闪退
    while True:
        try:
            main()
        except:
            time.sleep(5)