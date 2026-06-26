import os
import time
import logging
from flask import Blueprint, request, jsonify, render_template, make_response
from functools import wraps
from core.vwap_config_manager import VwapConfigManager
from core.vwap_bot import VWAPBot, PROJECT_ROOT
from utils.vwap_crypto import VwapCrypto
from core.vwap_broker import TossBroker

logger = logging.getLogger("vwap_bot")

vwap_bp = Blueprint('vwap', __name__, template_folder='templates')

# 전역 트레이딩 봇 인스턴스 생성 (가상 봇, 실제 봇 이중화)
virtual_bot = VWAPBot("VIRTUAL")
real_bot = VWAPBot("REAL")

def admin_required(f):
    """Admin 세션 토큰을 검증하는 API 데코레이터입니다."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        token = ""
        if auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        
        if not token:
            token = request.cookies.get('vwap_session', '')
            
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
    """가상/실제 봇의 실시간 상태 캐시 및 성과 지표(ROI 등)를 계산하여 반환합니다."""
    config = VwapConfigManager.load_config()
    
    # 1. 가상 자산 메트릭스 산출
    v_status = virtual_bot.get_status()
    v_trades = VwapConfigManager.load_trades("VIRTUAL")
    v_initial = float(config.get("virtual_initial_balance", 10000000.0))
    
    v_cash = v_status["cash"]
    v_holdings = v_status["holdings"]
    v_ticker = v_status["ticker"]
    
    v_stock_val = 0.0
    v_holding_info = v_holdings.get(v_ticker)
    if v_holding_info:
        v_stock_val = float(v_holding_info["qty"]) * v_status["current_price"]
        
    v_total_asset = v_cash + v_stock_val
    v_roi = ((v_total_asset - v_initial) / v_initial) * 100.0 if v_initial > 0 else 0.0
    v_unrealized_pnl = 0.0
    if v_holding_info:
        v_unrealized_pnl = (v_status["current_price"] - float(v_holding_info["entry_price"])) * float(v_holding_info["qty"])
        
    v_total_trades = len(v_trades)
    v_win_trades = [t for t in v_trades if t.get("pnl", 0.0) > 0]
    v_win_rate = (len(v_win_trades) / v_total_trades * 100.0) if v_total_trades > 0 else 0.0
    
    metrics_virtual = {
        "initial_balance": round(v_initial, 2),
        "total_asset": round(v_total_asset, 2),
        "cash": round(v_cash, 2),
        "stock_value": round(v_stock_val, 2),
        "unrealized_pnl": round(v_unrealized_pnl, 2),
        "roi": round(v_roi, 2),
        "win_rate": round(v_win_rate, 2),
        "total_trades": v_total_trades,
        "recent_trades": v_trades[-30:]
    }

    # 2. 실제 자산 메트릭스 산출 (Toss API 실시간 연동)
    r_status = real_bot.get_status()
    r_trades = VwapConfigManager.load_trades("REAL")
    r_initial = float(config.get("real_initial_balance", 10000000.0))
    
    # API 자격증명 등록 여부 확인
    api_active = bool(config.get("toss_client_id") and config.get("toss_client_secret"))
    
    r_cash = 0.0
    r_stock_val = 0.0
    r_holdings = {}
    r_unrealized_pnl = 0.0
    
    if api_active:
        # 실시간 자산 조회를 위해 TossBroker 활용
        try:
            # 기존 봇의 브로커가 있으면 재사용, 없으면 임시 생성
            broker = real_bot.real_broker
            if not broker:
                broker = TossBroker(
                    client_id=config["toss_client_id"],
                    client_secret=config["toss_client_secret"],
                    account_seq=config["toss_account_seq"]
                )
            
            # API 키가 가짜가 아닌 경우에만 실제 계좌 조회 시도
            if not broker.mock_mode:
                balance = broker.get_balance()
                r_cash = balance["cash"]
                
                # 실제 보유 종목들의 시세를 받아와 평가금액 계산
                for ticker, info in balance["holdings"].items():
                    current_price = broker.get_current_price(ticker)
                    if current_price <= 0 and r_status["ticker"] == ticker:
                        current_price = r_status["current_price"]
                    
                    qty = float(info["qty"])
                    entry_price = float(info["entry_price"])
                    eval_val = qty * current_price
                    pnl = (current_price - entry_price) * qty
                    
                    r_holdings[ticker] = {
                        "qty": round(qty, 4),
                        "entry_price": round(entry_price, 2),
                        "eval_value": round(eval_val, 2),
                        "pnl": round(pnl, 2),
                        "roi": round((pnl / (entry_price * qty) * 100.0) if entry_price > 0 else 0.0, 2)
                    }
                    r_stock_val += eval_val
                    r_unrealized_pnl += pnl
            else:
                # Mock 모드 폴백: 설정 캐시 활용
                api_active = False
                r_cash = r_status["cash"]
                r_holdings = r_status["holdings"]
                for t, info in r_holdings.items():
                    r_stock_val += float(info["qty"]) * r_status["current_price"]
        except Exception as e:
            logger.error(f"[api_status] 실제 자산 조회 에러: {e}")
            api_active = False
            r_cash = r_status["cash"]
            r_holdings = r_status["holdings"]
    else:
        # API 미설정 상태
        r_cash = 0.0
        r_holdings = {}
        
    r_total_asset = r_cash + r_stock_val
    r_roi = ((r_total_asset - r_initial) / r_initial) * 100.0 if r_initial > 0 else 0.0
    
    r_total_trades = len(r_trades)
    r_win_trades = [t for t in r_trades if t.get("pnl", 0.0) > 0]
    r_win_rate = (len(r_win_trades) / r_total_trades * 100.0) if r_total_trades > 0 else 0.0
    
    metrics_real = {
        "initial_balance": round(r_initial, 2),
        "total_asset": round(r_total_asset, 2),
        "cash": round(r_cash, 2),
        "stock_value": round(r_stock_val, 2),
        "unrealized_pnl": round(r_unrealized_pnl, 2),
        "roi": round(r_roi, 2),
        "win_rate": round(r_win_rate, 2),
        "total_trades": r_total_trades,
        "recent_trades": r_trades[-30:],
        "api_active": api_active and not (real_bot.real_broker and real_bot.real_broker.mock_mode),
        "holdings": r_holdings
    }

    return jsonify({
        "status": "success",
        "virtual_bot": v_status,
        "real_bot": r_status,
        "metrics": {
            "virtual": metrics_virtual,
            "real": metrics_real
        }
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
    
    # 가상 및 실제 설정을 모두 처리할 수 있도록 리스트 정의
    allowed_keys = [
        # 공통
        "toss_client_id", "max_daily_loss_limit",
        
        # 가상(VIRTUAL)용 키
        "virtual_ticker", "virtual_market", "virtual_interval", "virtual_n_percent", 
        "virtual_m_percent", "virtual_x_percent", "virtual_k_percent", "virtual_initial_balance", 
        "virtual_max_daily_loss_limit", "virtual_reset_time", "virtual_start_time", "virtual_use_adx_filter", 
        "virtual_adx_period", "virtual_adx_threshold", "virtual_use_rsi_filter", 
        "virtual_rsi_period", "virtual_rsi_threshold", "virtual_use_vwap_band", 
        "virtual_vwap_band_sigma",
        
        # 실제(REAL)용 키
        "real_ticker", "real_market", "real_interval", "real_n_percent", 
        "real_m_percent", "real_x_percent", "real_k_percent", "real_initial_balance", 
        "real_max_daily_loss_limit", "real_reset_time", "real_start_time", "real_use_adx_filter", 
        "real_adx_period", "real_adx_threshold", "real_use_rsi_filter", 
        "real_rsi_period", "real_rsi_threshold", "real_use_vwap_band", 
        "real_vwap_band_sigma"
    ]
    
    for key in allowed_keys:
        if key in new_data:
            # 적절한 형변환 수행
            if any(suffix in key for suffix in ["n_percent", "m_percent", "x_percent", "k_percent", "initial_balance", "max_daily_loss_limit", "adx_threshold", "rsi_threshold", "vwap_band_sigma"]):
                updated_config[key] = float(new_data[key])
            elif any(suffix in key for suffix in ["adx_period", "rsi_period"]):
                updated_config[key] = int(new_data[key])
            elif any(suffix in key for suffix in ["use_adx_filter", "use_rsi_filter", "use_vwap_band"]):
                val = new_data[key]
                if isinstance(val, str):
                    updated_config[key] = val.lower() == "true"
                else:
                    updated_config[key] = bool(val)
            else:
                updated_config[key] = str(new_data[key])

    # 민감 정보 필드는 마스킹이 아닌 새로 입력된 평문인 경우에만 덮어씀
    for key in ["toss_client_secret", "toss_account_seq"]:
        val = new_data.get(key, "")
        if val and not ("****" in val or val.count("*") >= 4):
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
    mode = data.get('mode', 'VIRTUAL').upper()
    
    target_bot = real_bot if mode == "REAL" else virtual_bot
    
    if action == 'start':
        success = target_bot.start()
        msg = f"{mode} 트레이딩 봇이 작동하기 시작했습니다." if success else f"{mode} 봇이 이미 실행 중입니다."
        return jsonify({"status": "success" if success else "failed", "message": msg})
        
    elif action == 'stop':
        success = target_bot.stop()
        msg = f"{mode} 트레이딩 봇이 중지되었습니다." if success else f"{mode} 봇이 실행 중이 아닙니다."
        return jsonify({"status": "success" if success else "failed", "message": msg})
        
    return jsonify({"status": "failed", "reason": "invalid_action"}), 400


@vwap_bp.route('/api/logs', methods=['GET'])
@admin_required
def api_get_logs():
    """선택한 모드의 트레이딩 봇 로그 파일의 최근 100줄을 스트리밍 형태로 반환합니다."""
    mode = request.args.get('mode', 'VIRTUAL').upper()
    log_path = os.path.join(PROJECT_ROOT, f"trading_bot_{mode.lower()}.log")
    
    if not os.path.exists(log_path):
        return jsonify({"status": "success", "logs": "로그 기록이 없습니다."})
        
    try:
        with open(log_path, "r", encoding="utf-8") as f:
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
    from backtest.vwap_backtester import VwapBacktester
    
    data = request.get_json() or {}
    ticker = data.get("ticker", "AAPL")
    interval = data.get("interval", "1m")
    n_percent = float(data.get("n_percent", 1.0))
    m_percent = float(data.get("m_percent", 1.0))
    x_percent = float(data.get("x_percent", 2.0))
    
    config = VwapConfigManager.load_config()
    initial_balance = float(data.get("initial_balance", config.get("real_initial_balance", 10000000.0)))
    
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
    """선택한 모드의 거래 체결 이력을 초기화하고 봇의 가상 브로커 잔고를 리셋합니다."""
    data = request.get_json() or {}
    mode = data.get('mode', 'VIRTUAL').upper()
    
    try:
        VwapConfigManager.save_trades([], mode)
        
        # 가상 거래 기록 초기화인 경우 봇의 virtual_broker 잔고 동기화 트리거
        if mode == "VIRTUAL" and virtual_bot.virtual_broker:
            virtual_bot.virtual_broker._sync_balance_from_trades()
            
        logger.info(f"🗑️ {mode} 거래 기록 및 평가 잔고가 성공적으로 초기화되었습니다.")
        return jsonify({"status": "success", "message": f"{mode} 거래 내역 및 평가 잔고가 초기화되었습니다."})
    except Exception as e:
        logger.error(f"거래 기록 초기화 실패: {e}")
        return jsonify({"status": "failed", "reason": str(e)}), 500
