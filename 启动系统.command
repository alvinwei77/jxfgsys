#!/bin/bash
# 废钢供应链系统 - 一键启动脚本
# 双击本文件即可：自动安装依赖（首次运行）→ 启动本地服务 → 自动打开浏览器
cd "$(dirname "$0")"

echo "=== 废钢供应链系统 启动中 ==="

if ! command -v python3 &>/dev/null; then
  echo "未检测到 Python 3，请先从 https://www.python.org/downloads/ 安装 Python 3，再重新双击本文件。"
  read -n 1 -s -r -p "按任意键关闭窗口..."
  exit 1
fi

echo "检查依赖（首次运行会自动安装 flask / openpyxl，可能需要十几秒）..."
python3 -m pip install --user -q flask openpyxl 2>/tmp/scrapmetal_pip_err.log \
  || python3 -m pip install --user --break-system-packages -q flask openpyxl 2>>/tmp/scrapmetal_pip_err.log

if ! python3 -c "import flask" &>/dev/null; then
  echo "依赖安装失败，错误信息如下，请截图给开发者："
  cat /tmp/scrapmetal_pip_err.log
  read -n 1 -s -r -p "按任意键关闭窗口..."
  exit 1
fi

if [ ! -f data/scrapmetal.db ]; then
  echo "首次运行，正在初始化数据库..."
  python3 init_db.py
fi

# 先关掉还在运行的旧版本（否则新版本抢不到 5000 端口，浏览器看到的还是旧页面，新页面会"找不到"）
OLD_PIDS=$( (lsof -ti tcp:5000 -sTCP:LISTEN; lsof -ti tcp:5080 -sTCP:LISTEN) 2>/dev/null | sort -u)
for p in $OLD_PIDS; do
  if ps -p "$p" -o command= 2>/dev/null | grep -q "app.py"; then
    echo "发现还在运行的旧版本（进程 $p），先关掉它……"
    kill "$p" 2>/dev/null
    sleep 1
  fi
done

echo "启动服务，稍后会自动打开浏览器（如果没有自动打开，请手动访问 http://127.0.0.1:5000 ；手机地址见「系统 → 手机访问」）"
( sleep 2 && open "http://127.0.0.1:5000" ) &

python3 app.py
