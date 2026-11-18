"""Finite retries and one process-wide chat concurrency budget. No secrets logged."""
import logging
import threading
import time
import httpx

logger = logging.getLogger('llm_transport')
CHAT_CONCURRENCY = 3
_slots = threading.BoundedSemaphore(CHAT_CONCURRENCY)


def retryable(error):
    status = getattr(error, 'status_code', None)
    body = getattr(error, 'body', None)
    if isinstance(body, dict):
        inner = body.get('error', body)
        if isinstance(inner, dict) and inner.get('code') in ('insufficient_quota', 'billing_hard_limit_reached'):
            return False
    return status in (408, 429, 500, 502, 503, 504) or any(
        name in type(error).__name__.lower() for name in ('timeout', 'connection'))


def request_chat(client, *, request_stage='字幕处理', request_timeout=120, attempts=2, **kwargs):
    kwargs.pop('timeout', None)
    model = str(kwargs.get('model', ''))
    waiting = time.monotonic()
    if not _slots.acquire(timeout=180):
        raise TimeoutError('等待LLM请求名额超过180秒，请减少同时运行的任务')
    try:
        elapsed = time.monotonic() - waiting
        if elapsed > 1:
            logger.info('LLM排队：阶段=%s 等待=%.1fs 本进程并发上限=%s', request_stage, elapsed, CHAT_CONCURRENCY)
        # Disable SDK retries so retry counts do not multiply invisibly.
        transport = client.with_options(max_retries=0,
            timeout=httpx.Timeout(float(request_timeout), connect=6.0))
        for attempt in range(1, attempts+1):
            started = time.monotonic()
            try:
                result = transport.chat.completions.create(**kwargs)
                logger.info('LLM成功：阶段=%s 模型=%s 尝试=%s/%s 用时=%.1fs',
                    request_stage, model, attempt, attempts, time.monotonic()-started)
                return result
            except Exception as error:
                again = attempt < attempts and retryable(error)
                logger.warning('LLM失败：阶段=%s 模型=%s 尝试=%s/%s 类型=%s 状态=%s 用时=%.1fs 将重试=%s',
                    request_stage, model, attempt, attempts, type(error).__name__,
                    getattr(error, 'status_code', None), time.monotonic()-started, again)
                if not again:
                    raise
                time.sleep(1.0)
    finally:
        _slots.release()
