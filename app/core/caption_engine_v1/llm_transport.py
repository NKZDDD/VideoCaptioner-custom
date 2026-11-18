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


def request_chat(client, *, request_stage='字幕处理', request_timeout=120, attempts=3, progress=None, **kwargs):
    def emit(message):
        if progress:
            try: progress(message)
            except Exception: pass
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
            emit(f'{request_stage}｜重试{attempt-1}/{attempts-1}｜等待模型返回')
            stop=threading.Event()
            def heartbeat():
                while not stop.wait(5):emit(f'{request_stage}｜重试{attempt-1}/{attempts-1}｜等待{int(time.monotonic()-started)}秒')
            ticker=threading.Thread(target=heartbeat,daemon=True);ticker.start()
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
                    raise RuntimeError(f'LLM分段重试已用尽或遇到不可重试错误：{request_stage}，共请求{attempt}次，原因{type(error).__name__}；已保留完成分段') from error
                emit(f'{request_stage}：临时连接失败，准备第 {attempt+1}/{attempts} 次请求')
                time.sleep(1.0)
            finally:
                stop.set();ticker.join(timeout=0.1)
    finally:
        _slots.release()
