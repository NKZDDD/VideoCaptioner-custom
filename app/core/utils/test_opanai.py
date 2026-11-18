import openai
import hashlib
import logging
import threading
import time
from app.core.utils.llm_transport import request_chat
from urllib.parse import urlsplit, urlunsplit


CHAT_TIMEOUT_SECONDS = 8
MODEL_TIMEOUT_SECONDS = 5
TASK_CHECK_TIMEOUT_SECONDS = 25
_success_checks = {}
_check_lock = threading.Lock()


def normalize_openai_base_url(base_url):
    """为只填写了域名的 OpenAI 兼容接口补上 /v1。"""
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme in ("http", "https") and parsed.path in ("", "/"):
        parsed = parsed._replace(path="/v1")
        return urlunsplit(parsed)
    return value


def format_openai_error(error, timeout_seconds=CHAT_TIMEOUT_SECONDS):
    """把 SDK 异常转换为用户可以直接处理的短消息。"""
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)

    messages = {
        400: "400：请求参数或模型不被该接口支持",
        401: "401：API Key 无效或已过期",
        403: "403：API Key 没有调用该模型的权限",
        404: "404：API 地址或模型名称不正确，请确认地址包含 /v1",
        429: "429：额度不足或请求过于频繁",
        500: "500：接口服务商内部错误",
        502: "502：接口服务商的上游模型不可用或过载",
        503: "503：接口服务暂时不可用",
        504: "504：接口服务商连接上游模型超时",
    }
    if status_code in messages:
        return messages[status_code]

    error_name = type(error).__name__.lower()
    if "timeout" in error_name:
        cause = type(getattr(error, '__cause__', None)).__name__.lower()
        stage = '建立连接' if 'connect' in cause else '等待接口响应'
        return f"{stage}超时：本次等待上限{timeout_seconds}秒；不代表模型一定不可用"
    if "connection" in error_name:
        return "连接失败：无法访问 API 地址，请检查地址、网络或服务状态"

    detail = str(error).strip().replace("\n", " ")
    if len(detail) > 240:
        detail = detail[:240] + "..."
    return detail or type(error).__name__


def test_openai(base_url, api_key, model, task_check=False):
    """
    这是一个测试OpenAI API的函数。
    它使用指定的API设置与OpenAI的GPT模型进行对话。

    参数:
    user_message (str): 用户输入的消息

    返回:
    bool: 是否成功
    str: 错误信息或者AI助手的回复
    """
    timeout = TASK_CHECK_TIMEOUT_SECONDS if task_check else CHAT_TIMEOUT_SECONDS
    try:
        base_url = normalize_openai_base_url(base_url)
        identity = hashlib.sha256((base_url + '\0' + api_key + '\0' + model).encode('utf-8')).hexdigest()
        with _check_lock:
            if task_check and time.monotonic() - _success_checks.get(identity, float('-inf')) < 120:
                logging.getLogger('llm_transport').info('LLM任务预检：复用两分钟内的成功结果')
                return True, '复用两分钟内同一接口、密钥和模型的成功检查'
        # 创建OpenAI客户端并发送请求到OpenAI API
        client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        )
        try:
            response = request_chat(client, request_stage='任务预检' if task_check else '连接检查',
                request_timeout=timeout, attempts=2 if task_check else 1,
                model=model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello!"},
                ],
                max_tokens=100,
            )
        finally:
            client.close()
        # 返回AI的回复
        if not getattr(response, "choices", None):
            return False, "接口返回格式异常：没有 Chat Completions 的 choices 字段"
        with _check_lock:
            _success_checks[identity] = time.monotonic()
        return True, str(response.choices[0].message.content)
    except Exception as e:
        message = format_openai_error(e, timeout)
        if api_key:
            message = message.replace(str(api_key), '[已隐藏密钥]')
        return False, message


def get_openai_models(base_url, api_key):
    try:
        base_url = normalize_openai_base_url(base_url)
        # 创建OpenAI客户端并获取模型列表
        models = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=MODEL_TIMEOUT_SECONDS,
            max_retries=0,
        ).models.list()

        # 根据不同模型设置权重进行排序
        def get_model_weight(model_name):
            model_name = model_name.lower()
            if model_name.startswith(("gpt-4o", "claude-3-5")):
                return 10
            elif model_name.startswith("gpt-4"):
                return 5
            elif model_name.startswith("claude-3"):
                return 6
            elif model_name.startswith(("deepseek", "glm")):
                return 3
            return 0

        sorted_models = sorted(
            [model.id for model in models], key=lambda x: (-get_model_weight(x), x)
        )
        return sorted_models
    except Exception:
        return []
