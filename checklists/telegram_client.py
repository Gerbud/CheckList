import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from urllib import error, parse, request

from checklists.models import TelegramSystemSettings


logger = logging.getLogger(__name__)
OFFICIAL_API_BASE_URL = 'https://api.telegram.org'
TELEGRAM_API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 TelegramGateway/1.0',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}
GET_METHODS = {
    'getMe',
    'getChat',
    'getUpdates',
    'getWebhookInfo',
    'getFile',
}
SUPPORTED_METHODS = {
    'sendMessage',
    'editMessageText',
    'answerCallbackQuery',
    'getMe',
    'getChat',
    'getUpdates',
    'setWebhook',
    'getWebhookInfo',
    'deleteWebhook',
    'deleteMessage',
    'setMyCommands',
    'getMyCommands',
    'createForumTopic',
    'closeForumTopic',
    'reopenForumTopic',
    'editForumTopic',
    'deleteForumTopic',
    'getFile',
}


@dataclass(frozen=True)
class TelegramResponse:
    data: dict
    alternative_attempts: int
    official_attempts: int


class TelegramAPIError(Exception):
    """Безопасная ошибка Telegram без токена, URL и сырого ответа."""

    def __init__(
        self,
        message,
        *,
        alternative_attempts=0,
        official_attempts=0,
        retryable=True,
        status_code=None,
    ):
        super().__init__(message)
        self.alternative_attempts = alternative_attempts
        self.official_attempts = official_attempts
        self.retryable = retryable
        self.status_code = status_code


def _parse_response_json(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _safe_description(value, token):
    description = str(value or '').replace(token, '[REDACTED]')
    return ' '.join(description.split())[:500]


def _safe_error_details(*, token, status=None, data=None, fallback):
    details = []
    if status is not None:
        details.append(f'http_status={status}')
    if isinstance(data, dict) and data.get('error_code') is not None:
        details.append(f"error_code={data['error_code']}")
    description = (
        data.get('description')
        if isinstance(data, dict)
        else None
    ) or fallback
    safe_description = _safe_description(description, token)
    if safe_description:
        details.append(f'description={safe_description}')
    return '; '.join(details) or 'description=Telegram request failed'


def _build_api_request(endpoint, method, payload):
    if method in GET_METHODS:
        query = parse.urlencode(payload, doseq=True)
        url = f'{endpoint}?{query}' if query else endpoint
        return request.Request(
            url,
            headers=TELEGRAM_API_HEADERS,
            method='GET',
        )
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return request.Request(
        endpoint,
        data=body,
        headers=TELEGRAM_API_HEADERS,
        method='POST',
    )


def _safe_attempt(base_url, token, method, payload, timeout):
    endpoint = f"{base_url.rstrip('/')}/bot{token}/{method}"
    api_request = _build_api_request(endpoint, method, payload)
    try:
        with request.urlopen(api_request, timeout=timeout) as response:
            status = getattr(response, 'status', response.getcode())
            raw = response.read()
    except error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b''
        return None, _safe_error_details(
            token=token,
            status=exc.code,
            data=_parse_response_json(raw),
            fallback='HTTP error',
        )
    except (TimeoutError, socket.timeout):
        return None, _safe_error_details(
            token=token,
            fallback='request timeout',
        )
    except error.URLError:
        return None, _safe_error_details(
            token=token,
            fallback='network error',
        )
    except Exception:
        return None, _safe_error_details(
            token=token,
            fallback='transport error',
        )
    data = _parse_response_json(raw)
    if not 200 <= int(status) < 300:
        return None, _safe_error_details(
            token=token,
            status=status,
            data=data,
            fallback='HTTP error',
        )
    if data is None:
        return None, _safe_error_details(
            token=token,
            status=status,
            fallback='invalid JSON',
        )
    if data.get('ok') is not True:
        return None, _safe_error_details(
            token=token,
            status=status,
            data=data,
            fallback='Telegram API returned ok=false',
        )
    return data, None


def send_telegram_request(
    method,
    payload,
    *,
    system_settings=None,
    incoming=False,
    quick=False,
    retry_on_failure=True,
    sleeper=time.sleep,
):
    if method not in SUPPORTED_METHODS:
        raise TelegramAPIError('Метод Telegram API не поддерживается.')
    if not isinstance(payload, dict):
        raise TelegramAPIError('Payload Telegram должен быть объектом.')
    config = system_settings or TelegramSystemSettings.get_solo()
    if not config.is_enabled or not config.bot_token:
        raise TelegramAPIError('Telegram-интеграция не настроена.')

    routes = []
    if not incoming and (quick or config.use_alternative_gateway):
        routes.append(
            (
                config.alternative_api_base_url,
                1 if quick else config.alternative_attempts,
                'alternative',
            )
        )
    if (
        incoming
        or not config.use_alternative_gateway
        or config.fallback_to_official_api
    ):
        routes.append(
            (
                OFFICIAL_API_BASE_URL,
                1 if incoming else config.official_attempts,
                'official',
            )
        )

    if not retry_on_failure and routes:
        routes = [(routes[0][0], 1, routes[0][2])]

    alternative_attempts = 0
    official_attempts = 0
    last_error = 'delivery failed'
    for route_index, (base_url, attempts, route_name) in enumerate(routes):
        for attempt_index in range(attempts):
            if route_name == 'alternative':
                alternative_attempts += 1
            else:
                official_attempts += 1
            data, attempt_error = _safe_attempt(
                base_url,
                config.bot_token,
                method,
                payload,
                min(config.request_timeout_seconds, 1.5)
                if quick
                else config.request_timeout_seconds,
            )
            if data is not None:
                return TelegramResponse(
                    data=data,
                    alternative_attempts=alternative_attempts,
                    official_attempts=official_attempts,
                )
            last_error = attempt_error
            logger.warning(
                'Telegram %s attempt %s failed: %s.',
                route_name,
                attempt_index + 1,
                attempt_error,
            )
            has_more = (
                attempt_index + 1 < attempts or route_index + 1 < len(routes)
            )
            if has_more and config.retry_delay_seconds and not quick:
                sleeper(config.retry_delay_seconds)
    raise TelegramAPIError(
        f'Telegram delivery failed: {last_error}.',
        alternative_attempts=alternative_attempts,
        official_attempts=official_attempts,
        status_code=(
            int(match.group(1))
            if (match := re.search(r'http_status=(\d+)', last_error or ''))
            else None
        ),
        retryable=not (
            (match := re.search(r'http_status=(\d+)', last_error or ''))
            and 400 <= int(match.group(1)) < 500
        ),
    )
