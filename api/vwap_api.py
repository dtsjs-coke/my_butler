import os
import time
import logging
from flask import Blueprint, request, jsonify, render_template, make_response
from functools import wraps
from core.vwap_config_manager import VwapConfigManager
from core.vwap_bot import VWAPBot, LOG_PATH
from utils.vwap_crypto import VwapCrypto

logger = logging.getLogger("vwap_bot")

vwap_bp = Blueprint('vwap', __name__, template_folder='templates')

# 전역 트레이딩 봇 인스턴스 생성
vwap_bot = VWAPBot()

def admin_required(f):
    """Admin 세션 토큰을 검증하는 API 데코레이터입니다."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. 헤더에서 토큰 추출
        auth_header = request.headers.get('Authorization', '')
        token = ""
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        # 2. 헤더에 없으면 쿠키에서 추출
        if not token:
            token = request.cookies.get('vwap_session', '')
            
        # 3. 토큰 검증 (만료시간 24시간)
        if not VwapCrypto.verify_session_token(token, max_age_seconds=86400):
            return jsonify({"status": "failed", "reason": "unauthorized"}), 401
            
        return f(*args, **kwargs)
    return decorated


@vwap_bp.route('/')
def vwap_home():
    """로그인 상태면 대시보드를, 아니면 로그인 게이트웨이 화면을 서빙합니다."""
    token = request.cookies.get('vwap_session', '')
    if VwapCrypto.verify_session_token(token):
        return render_template('vwap_dashboard.html')
    return render_template('vwap_login.html')


@vwap_bp.route('/login', methods=['POST'])
def vwap_login():
    """Admin 비밀번호를 검증하여 암호화된 세션 토큰 쿠키를 발급합니다."""
    data = request.get_json() or {}
    password = data.get('password', '')
    
    config = VwapConfigManager.load_config()
    pw_hash = VwapCrypto.hash_password(password)
    
    if pw_hash == config.get("admin_password_hash"):
        token = VwapCrypto.generate_session_token("admin")
        
        response = make_response(jsonify({"status": "success", "token": token}))
        # 쿠키 유효기간 1일 설정 (자바스크립트 접근 가능하게 HttpOnly 옵션 미부여하여 클라이언트 통신 편의 도모)
        response.set_cookie('vwap_session', token, max_age=86400, path='/')
        return response
    else:
        return jsonify({"status": "failed", "reason": "invalid_password"}), 401


@vwap_bp.route('/logout', methods=['POST'])
def vwap_logout():
    """로그아웃 처리하고 세션 쿠키를 만료시킵니다."""
    response = make_response(jsonify({"status": "success"}))
    response.set_cookie('vwap_session', '', expires=0, path='/')
    return response


@vwap_bp.route('/api/status', methods=['GET'])
@admin_required
def api_get_status():
    """트레이딩 봇의 실시간 상태 캐시 및 성과 지표(ROI 등)를 계산하여 반환합니다."""
    bot_status = vwap_bot.get_status().copy()
    trades = VwapConfigManager.load_trades()
    
    # 설정 로드하여 초기 원금 파악
    config = VwapConfigManager.load_config()
    initial_balance = float(config.get("initial_balance", 10000000.0))
    
    # 1. 평가 자산 재산출
    cash = bot_status["cash"]
    holdings = bot_status["holdings"]
    ticker = bot_status["ticker"]
    
    stock_value = 0.0
    holding_info = holdings.get(ticker)
    if holding_info:
        # 평가 금액 = 보유 수량 * 현재가
        stock_value = float(holding_info["qty"]) * bot_status["current_price"]
        
    total_asset = cash + stock_value
    roi = ((total_asset - initial_balance) / initial_balance) * 100.0 if initial_balance > 0 else 0.0
    
    # 2. 미실현 손익 계산
    unrealized_pnl = 0.0
    if holding_info:
        unrealized_pnl = (bot_status["current_price"] - float(holding_info["entry_price"])) * float(holding_info["qty"])

    # 3. 최근 거래 기록 통계 추출
    total_trades_count = len(trades)
    win_trades = [t for t in trades if t.get("pnl", 0.0) > 0]
    win_rate = (len(win_trades) / total_trades_count * 100.0) if total_trades_count > 0 else 0.0

    metrics = {
        "initial_balance": round(initial_balance, 2),
        "total_asset": round(total_asset, 2),
        "stock_value": round(stock_value, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "roi": round(roi, 2),
        "win_rate": round(win_rate, 2),
        "total_trades": total_trades_count,
        "recent_trades": trades[-30:] # 최근 30개 거래 내역
    }
    
    is_mock = vwap_bot.real_broker.mock_mode if vwap_bot.real_broker else True
    return jsonify({
        "status": "success",
        "bot": bot_status,
        "metrics": metrics,
        "is_mock_mode": is_mock
    })


@vwap_bp.route('/api/config', methods=['GET', 'POST'])
@admin_required
def api_config():
    """트레이딩 봇의 설정을 조회하거나 수정합니다."""
    if request.method == 'GET':
        config = VwapConfigManager.load_config()
        
        # 민감 정보 마스킹하여 보안 강화
        masked_config = config.copy()
        if masked_config.get("toss_client_secret"):
            secret = masked_config["toss_client_secret"]
            masked_config["toss_client_secret"] = secret[:4] + "*" * (len(secret) - 4) if len(secret) > 4 else "****"
        if masked_config.get("toss_account_seq"):
            seq = masked_config["toss_account_seq"]
            masked_config["toss_account_seq"] = seq[:4] + "*" * (len(seq) - 4) if len(seq) > 4 else "****"
            
        # 비밀번호 해시는 외부로 절대 노출 금지
        masked_config.pop("admin_password_hash", None)
        return jsonify({"status": "success", "config": masked_config})

    # POST (설정 업데이트)
    new_data = request.get_json() or {}
    current_config = VwapConfigManager.load_config()
    
    # 덮어쓸 설정 생성
    updated_config = current_config.copy()
    
    allowed_keys = [
        "mode", "ticker", "market", "interval", "n_percent", 
        "m_percent", "x_percent", "k_percent", "initial_balance", 
        "reset_time", "toss_client_id", "max_daily_loss_limit"
    ]
    
    for key in allowed_keys:
        if key in new_data:
            # 적절한 형변환 수행
            if key in ["n_percent", "m_percent", "x_percent", "k_percent", "initial_balance", "max_daily_loss_limit"]:
                updated_config[key] = float(new_data[key])
            else:
                updated_config[key] = str(new_data[key])

    # 민감 정보 필드는 마스킹이 아닌 새로 입력된 평문인 경우에만 덮어씀
    for key in ["toss_client_secret", "toss_account_seq"]:
        val = new_data.get(key, "")
        if val and "*" not in val:  # '*' 문자 포함은 마스킹 데이터이므로 변경 안함
            updated_config[key] = val

    # Admin 패스워드 직접 변경 요청 처리
    new_pw = new_data.get("new_admin_password", "")
    if new_pw:
        updated_config["admin_password_hash"] = VwapCrypto.hash_password(new_pw)

    # 영구 저장
    VwapConfigManager.save_config(updated_config)
    return jsonify({"status": "success", "message": "설정이 성공적으로 저장되었습니다."})


@vwap_bp.route('/api/control', methods=['POST'])
@admin_required
def api_control():
    """백그라운드 봇을 시작하거나 정지시킵니다."""
    data = request.get_json() or {}
    action = data.get('action', '')
    
    if action == 'start':
        success = vwap_bot.start()
        msg = "트레이딩 봇이 작동하기 시작했습니다." if success else "봇이 이미 실행 중입니다."
        return jsonify({"status": "success" if success else "failed", "message": msg})
        
    elif action == 'stop':
        success = vwap_bot.stop()
        msg = "트레이딩 봇이 중지되었습니다." if success else "봇이 실행 중이 아닙니다."
        return jsonify({"status": "success" if success else "failed", "message": msg})
        
    return jsonify({"status": "failed", "reason": "invalid_action"}), 400


@vwap_bp.route('/api/logs', methods=['GET'])
@admin_required
def api_get_logs():
    """트레이딩 봇 로그 파일(trading_bot.log)의 최근 100줄을 실시간 스트리밍 형태로 반환합니다."""
    if not os.path.exists(LOG_PATH):
        return jsonify({"status": "success", "logs": "로그 기록이 없습니다."})
        
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        recent_lines = lines[-100:]
        logs_text = "".join(recent_lines)
        return jsonify({"status": "success", "logs": logs_text})
    except Exception as e:
        return jsonify({"status": "failed", "reason": str(e)})


@vwap_bp.route('/api/backtest', methods=['POST'])
@admin_required
def api_run_backtest():
    """토스 API 과거 봉 데이터를 활용해 백테스트를 실행하고 결과를 요약 반환합니다."""
    # (후순위로 구현될 백테스터 컴포넌트를 연결하는 다리)
    from backtest.vwap_backtester import VwapBacktester
    
    data = request.get_json() or {}
    ticker = data.get("ticker", "AAPL")
    interval = data.get("interval", "1m")
    n_percent = float(data.get("n_percent", 1.0))
    m_percent = float(data.get("m_percent", 1.0))
    x_percent = float(data.get("x_percent", 2.0))
    initial_balance = float(data.get("initial_balance", 10000000.0))
    
    # Toss API를 연동하여 백테스트를 수행하므로 토큰 정보 전달을 위해 TossBroker를 통해 시세를 읽음
    config = VwapConfigManager.load_config()
    from core.vwap_broker import TossBroker
    toss = TossBroker(
        client_id=config["toss_client_id"],
        client_secret=config["toss_client_secret"],
        account_seq=config["toss_account_seq"]
    )
    
    try:
        logger.info(f"⚡ [{ticker}] 백테스트 연산 시작 요청 접수...")
        result = VwapBacktester.run(toss, ticker, interval, n_percent, m_percent, x_percent, initial_balance)
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        logger.error(f"백테스트 연산 실패: {e}")
        return jsonify({"status": "failed", "reason": str(e)})


@vwap_bp.route('/api/reset-trades', methods=['POST'])
@admin_required
def api_reset_trades():
    """가상 거래 체결 이력을 초기화하고 봇의 가상 브로커 잔고를 리셋합니다."""
    try:
        # 1. vwap_trades.json을 빈 배열로 초기화
        VwapConfigManager.save_trades([])
        
        # 2. 봇의 virtual_broker 잔고 동기화 트리거
        if vwap_bot.virtual_broker:
            vwap_bot.virtual_broker._sync_balance_from_trades()
            
        logger.info("🗑️ 가상 거래 기록 및 평가 잔고가 성공적으로 초기화되었습니다.")
        return jsonify({"status": "success", "message": "가상 거래 내역 및 평가 잔고가 초기화되었습니다."})
    except Exception as e:
        logger.error(f"거래 기록 초기화 실패: {e}")
        return jsonify({"status": "failed", "reason": str(e)}), 500
