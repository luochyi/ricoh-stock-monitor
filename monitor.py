#!/usr/bin/env python3
"""理光库存监测：真实数据入口需先核实，再配置字段映射。"""
import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from operating_window import QuietHours, require_active


class MonitorError(Exception):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise MonitorError('无法读取 JSON 文件，请检查路径、UTF-8 编码及格式') from None


def pick(value, path):
    try:
        for key in path.split('.') if path else []:
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise MonitorError('响应缺少配置字段，拒绝更新库存状态') from None


def normalize(data, source):
    if source.get('mode') == 'ricoh_refurbished_list':
        # 官翻品接口成功返回空数组即无货；签名错误不得当作空库存。
        if (not isinstance(data, dict) or type(data.get('code')) is not int
                or data['code'] != 0 or not isinstance(data.get('data'), list)):
            raise MonitorError('小程序库存查询失败：签名可能缺失或过期，保留上次状态')
        target = source.get('target_name')
        available = False
        products = {}
        all_models = source.get('scope') == 'all'
        for row in data['data']:
            if (not isinstance(row, dict) or not isinstance(row.get('storeName'), str)
                    or not row['storeName'].strip()):
                raise MonitorError('商品名称字段异常，保留上次状态')
            if not all_models and target and row['storeName'].strip() != target:
                continue
            stock = row.get('stock')
            if type(stock) is not int or stock < 0:
                raise MonitorError('商品实际库存字段异常，保留上次状态')
            available = available or stock > 0
            if all_models:
                product_id = row.get('id')
                if type(product_id) is not int or product_id <= 0 or str(product_id) in products:
                    raise MonitorError('商品 ID 无效或重复，拒绝更新部分库存')
                products[str(product_id)] = {'name': row['storeName'], 'stock': stock,
                                              'url': '', 'listed': True}
        if all_models:
            return products
        return {'ricoh-refurbished-sid9': {
            'name': target or '理光官翻品',
            'stock': available, 'url': ''}}
    guard = source.get('success')
    if not guard or 'path' not in guard or 'equals' not in guard:
        raise MonitorError('必须配置接口成功标志 success，避免把登录失效当成库存')
    value = pick(data, guard['path'])
    if type(value) is not type(guard['equals']) or value != guard['equals']:
        raise MonitorError('库存接口未返回成功标志，可能登录失效或被限制')
    rows = pick(data, source['items_path'])
    if not isinstance(rows, list) or (not rows and not source.get('allow_empty', False)):
        raise MonitorError('商品列表为空或格式不正确；保留上次库存')
    fields = source['fields']
    mode = source.get('stock_mode', 'quantity')
    if mode not in ('quantity', 'available'):
        raise MonitorError('stock_mode 必须为 quantity 或 available')
    result = {}
    for row in rows:
        sku, name, stock = (pick(row, fields[key]) for key in ('id', 'name', 'stock'))
        if isinstance(sku, bool) or not isinstance(sku, (str, int)) or not str(sku).strip():
            raise MonitorError('商品/SKU 标识无效')
        if not isinstance(name, str) or not name.strip():
            raise MonitorError('商品名称无效')
        sku = str(sku)
        if sku in result:
            raise MonitorError('SKU 重复，请使用唯一规格标识或检查分页配置')
        if mode == 'available':
            if type(stock) is not bool:
                raise MonitorError('有货模式要求 JSON 布尔值')
        else:
            if isinstance(stock, str) and re.fullmatch(r'[0-9]+', stock):
                stock = int(stock)
            if type(stock) is not int or stock < 0:
                raise MonitorError('库存必须是非负整数，拒绝将未知值当成零')
        url = pick(row, fields['url']) if fields.get('url') else ''
        if not isinstance(url, str):
            raise MonitorError('商品链接必须是字符串')
        result[sku] = {'name': name, 'stock': stock, 'url': url}
    ids = source.get('include_ids', [])
    if ids:
        result = {key: item for key, item in result.items() if key in ids}
    return result


def changes(old, now):
    events = []
    for sku, item in now.items():
        previous = old.get(sku)
        stock = item['stock']
        if previous is None:
            kind = '新品上架'
        elif previous.get('listed') is False and item.get('listed') is True:
            kind = '重新上架'
        elif previous['stock'] == stock:
            continue
        elif previous['stock'] == 0 and stock > 0:
            kind = '补货'
        elif stock == 0:
            kind = '售罄'
        else:
            kind = '库存变化'
        events.append(dict(item, id=sku, kind=kind,
                           before=previous['stock'] if previous else None))
    return events


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录原子替换，避免进程中断产生半个状态文件。
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stock_label(stock):
    if stock is None:
        return '未记录'
    if type(stock) is bool:
        return '有货' if stock else '无货'
    return str(stock)


def format_message(events):
    lines = ['理光库存提醒｜官翻品', time.strftime('%Y-%m-%d %H:%M:%S %z')]
    for event in events:
        lines.append('\n[%s] %s\nSKU：%s\n库存：%s → %s' % (
            event['kind'], event['name'][:160], event['id'][:80],
            stock_label(event['before']), stock_label(event['stock'])))
        if event['url'].startswith(('https://', 'http://')):
            lines.append(event['url'][:400])
    return '\n'.join(lines)


def stock_notification_enabled(name, policy):
    if not policy.get('mute_plain_gr4', False):
        return True
    # 统一全角字符、罗马数字及型号分隔符；HDF 优先保留通知。
    name = unicodedata.normalize('NFKC', name).casefold()
    name = re.sub(r'[\s_-]+', '', name)
    return 'hdf' in name or not re.search(r'gr(?:iv|4)(?![a-z0-9])', name)


def filter_pending_stock_message(message, policy):
    if not policy.get('mute_plain_gr4', False) or not message.startswith('理光库存提醒｜官翻品\n'):
        return message
    # 旧队列保存的是已排版文本，逐条过滤，避免混合消息中的 HDF 一起丢失。
    parts = re.split(r'\n\n(?=\[)', message)
    if len(parts) == 1:
        return message
    kept = []
    for part in parts[1:]:
        match = re.match(r'\[[^\]\n]+\] ([^\n]+)\nSKU：', part)
        if not match or stock_notification_enabled(match.group(1), policy):
            kept.append(part)
    return '\n\n'.join([parts[0]] + kept) if kept else ''


def cycle(config, path, fetch, send):
    state = read_json(path) if Path(path).exists() else {
        'version': 1, 'initialized': False, 'items': {}, 'pending': []}
    if (not isinstance(state, dict) or state.get('version') != 1
            or type(state.get('initialized')) is not bool
            or not isinstance(state.get('items'), dict)
            or not isinstance(state.get('pending'), list)):
        raise MonitorError('状态文件损坏或版本不符，请保留备份后检查')
    policy = config.get('notification_policy', {})
    pending = [filter_pending_stock_message(message, policy) for message in state['pending']]
    pending = [message for message in pending if message]
    if pending != state['pending']:
        state['pending'] = pending
        save_state(path, state)
    # 先重发持久化消息，发送失败不丢失短暂出现的补货事件。
    def flush():
        while state['pending']:
            send(state['pending'][0])
            state['pending'].pop(0)
            save_state(path, state)
    flush()
    now = normalize(fetch(), config['source'])
    if config['source'].get('scope') == 'all' and 'ricoh-refurbished-sid9' in state['items']:
        # 旧版聚合库存没有商品 ID，完整查询成功后建立各型号基线，保留待发通知。
        state['items'].pop('ricoh-refurbished-sid9')
        state['initialized'] = False
    events = changes(state['items'], now)
    if not state['initialized']:
        events = [dict(event, kind='首次发现有货') for event in events
                  if event['stock'] > 0 and config.get('notify_initial_available', True)]
    # 列表消失不代表库存为零；单独标记以便重新上架时通知。
    if config['source'].get('scope') == 'all':
        for sku, item in state['items'].items():
            if sku not in now:
                item['listed'] = False
    state['items'].update(now)
    state['initialized'] = True
    # 静默只影响通知，仍记录库存基线，不改变自动下单的数据源和规则。
    events = [event for event in events if stock_notification_enabled(event['name'], policy)]
    for offset in range(0, len(events), 5):
        state['pending'].append(format_message(events[offset:offset + 5]))
    save_state(path, state)
    flush()
    logging.info('检查成功：%s 个 SKU，%s 个变化', len(now), len(events))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不把库存授权头或机器人密钥转发到重定向地址。
        raise MonitorError('接口发生重定向，请检查登录状态或更新接口地址')


def request_json(url, method='GET', body=None, headers=None):
    # 库存、订单、钉钉共用最终发送入口，防止白天开始的一轮跨过 18 点后继续发请求。
    require_active()
    if urllib.parse.urlsplit(url).scheme not in ('https', 'http'):
        raise MonitorError('接口地址必须为 HTTP(S)')
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=encoded, method=method, headers={
        'Accept': 'application/json', 'Content-Type': 'application/json; charset=utf-8',
        **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise MonitorError('接口响应超过 4MB，请检查分页')
        return json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise MonitorError('HTTP %s：请检查接口权限或访问频率' % exc.code) from None
    except urllib.error.URLError:
        raise MonitorError('连接失败，请检查网络、地址和 HTTPS 证书') from None
    except (ValueError, UnicodeError):
        raise MonitorError('响应不是有效 UTF-8 JSON，可能是登录页或访问限制页') from None
    except (TimeoutError, OSError):
        raise MonitorError('请求超时或网络中断') from None


def signed_url(webhook, secret, timestamp=None):
    if not secret:
        return webhook
    timestamp = str(timestamp if timestamp is not None else int(time.time() * 1000))
    signature = base64.b64encode(hmac.new(secret.encode('utf-8'),
        (timestamp + '\n' + secret).encode('utf-8'), hashlib.sha256).digest()).decode('ascii')
    parts = urllib.parse.urlsplit(webhook)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query)
             if key not in ('timestamp', 'sign')]
    query += [('timestamp', timestamp), ('sign', signature)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def webhook_config():
    webhook = os.environ.get('DINGTALK_WEBHOOK', '')
    parts = urllib.parse.urlsplit(webhook)
    if (parts.scheme != 'https' or parts.netloc != 'oapi.dingtalk.com'
            or parts.path != '/robot/send'
            or not urllib.parse.parse_qs(parts.query).get('access_token')):
        raise MonitorError('请设置有效的 DINGTALK_WEBHOOK 环境变量')
    return webhook, os.environ.get('DINGTALK_SECRET', '')


def send_dingtalk(message):
    require_active()
    webhook, secret = webhook_config()
    result = request_json(signed_url(webhook, secret), method='POST', body={
        'msgtype': 'text', 'text': {'content': message}, 'at': {'isAtAll': False}})
    if not isinstance(result, dict) or type(result.get('errcode')) is not int or result['errcode'] != 0:
        raise MonitorError('钉钉拒绝消息，请检查加签、关键词、IP 白名单或限流；消息已保留')
    # 分批发送时留出间隔，避免批量上架瞬间打满机器人频率。
    time.sleep(3.1)


def ricoh_headers(url, timestamp=None, nonce=None):
    """与理光小程序第 12 版的库存 GET 查询签名保持一致。"""
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'shop.ricn-mall.com'
            or parts.path != '/app-api/product/products'):
        raise MonitorError('理光签名适配器仅支持已核实的商品列表查询')
    stamp = str(timestamp if timestamp is not None else int(time.time() * 1000))
    nonce = nonce if nonce is not None else stamp + secrets.token_hex(8)
    # 请求 URL 的查询串与签名输入严格一致；GET 请求体为空。
    content = '\n'.join(['miniapp-product-sign', 'GET', '/product/products',
                          parts.query, '', stamp, nonce])
    return {'X-Request-Timestamp': stamp, 'X-Request-Nonce': nonce,
            'X-Request-Sign': hashlib.sha256(content.encode('utf-8')).hexdigest()}


def source_fetch(config):
    source = config['source']
    url = source.get('url', '')
    if not url:
        raise MonitorError('尚未配置真实库存接口 source.url；小程序接口仍需核实')
    if not source.get('complete_inventory_confirmed', False):
        raise MonitorError('请核实接口覆盖完整官翻品库存/指定 SKU 后设置 complete_inventory_confirmed')
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise MonitorError('真实库存接口必须使用 HTTPS')
    headers = {}
    for name, env_name in source.get('headers_env', {}).items():
        value = os.environ.get(env_name)
        if not value:
            raise MonitorError('库存请求所需环境变量未设置：' + env_name)
        headers[name] = value
    method = source.get('method', 'GET').upper()
    if method not in ('GET', 'POST'):
        raise MonitorError('仅支持 GET 或 POST 库存查询')
    if source.get('mode') == 'ricoh_refurbished_list':
        if method != 'GET' or source.get('body') is not None:
            raise MonitorError('官翻品模式只支持无请求体的 GET 查询')
        if source.get('scope') == 'all':
            parts = urllib.parse.urlsplit(url)
            query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            if (query.get('sid') != ['9'] or query.get('keyword', ['']) != ['']
                    or query.get('page') != ['1'] or source.get('target_name')
                    or any(query.get(key, ['']) != [''] for key in ('couponId', 'news', 'type'))):
                raise MonitorError('全型号监测必须从官翻品分类第 1 页开始且不设置商品筛选')
            try:
                limit = int(query['limit'][0])
                if len(query['limit']) != 1 or not 1 <= limit <= 100:
                    raise ValueError()
            except (KeyError, ValueError, IndexError):
                raise MonitorError('官翻品分页大小必须为 1 到 100') from None
            rows, ids = [], set()
            for page in range(1, 101):
                page_query = [(key, str(page) if key == 'page' else value)
                              for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
                page_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(page_query)))
                result = request_json(page_url, method, None, {**headers, **ricoh_headers(page_url)})
                products = normalize(result, source)
                if ids.intersection(products):
                    raise MonitorError('分页商品重复，完整库存无法确认，稍后重试')
                ids.update(products)
                rows.extend(result['data'])
                # 以空页结束，避免服务端限制单页条数导致漏读后续商品。
                if not products:
                    return {'code': 0, 'data': rows}
            raise MonitorError('官翻品分页超过安全上限，拒绝使用不完整库存')
        headers.update(ricoh_headers(url))
    return request_json(url, method, source.get('body'), headers)


@contextmanager
def exclusive_lock(path):
    # 文件锁由操作系统在进程退出后释放，防止多实例重复通知。
    lock = Path(str(path) + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+', encoding='utf-8') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.write('0')
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorError('已有实例使用此状态文件，请勿重复启动') from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--once', action='store_true', help='检查一次后退出')
    parser.add_argument('--dry-run', action='store_true', help='只读检查，不发送消息、不写状态')
    parser.add_argument('--fixture', help='仅用于 dry-run 的模拟 JSON 文件')
    parser.add_argument('--test-notify', action='store_true', help='主动向钉钉发送一条测试消息')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.fixture and not args.dry_run:
        parser.error('--fixture 必须配合 --dry-run，防止模拟库存触发真实消息')
    if args.test_notify and (args.dry_run or args.fixture):
        parser.error('--test-notify 不可与只读/模拟模式同时使用')
    try:
        if args.test_notify:
            send_dingtalk('理光库存提醒：机器人连接测试，本消息不代表实际有货。')
            logging.info('钉钉接口已接受测试消息')
            return 0
        config_path = Path(args.config).resolve()
        config = read_json(config_path)
        if not isinstance(config, dict) or not isinstance(config.get('source'), dict):
            raise MonitorError('配置必须包含 source 对象')
        def fetch():
            return read_json(args.fixture) if args.fixture else source_fetch(config)
        if args.dry_run:
            items = normalize(fetch(), config['source'])
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        # 缺少接入配置时立即退出，避免空跑造成正在监控的错觉。
        if not config['source'].get('url') or not config['source'].get('complete_inventory_confirmed'):
            raise MonitorError('库存入口尚未接入，请先配置并核实 source.url 和完整库存覆盖')
        webhook_config()
        interval = config.get('interval_seconds', 60)
        if type(interval) is not int or interval < 30:
            raise MonitorError('interval_seconds 必须是至少 30 的整数')
        state_path = config_path.parent / config.get('state_file', 'state.json')
        failures = 0
        with exclusive_lock(state_path):
            while True:
                try:
                    cycle(config, state_path, fetch, send_dingtalk)
                    failures = 0
                except MonitorError as exc:
                    logging.error('%s', exc)
                    failures += 1
                    if args.once:
                        return 1
                if args.once:
                    return 0
                delay = max(interval, min(interval * (2 ** min(failures, 6)), 1800))
                if failures:
                    logging.warning('连续失败 %s 次，%s 秒后重试；当前库存未核实', failures, delay)
                time.sleep(delay)
    except (MonitorError, KeyError, TypeError, ValueError, OSError) as exc:
        logging.error('%s', exc if isinstance(exc, MonitorError) else '配置、状态或文件操作异常，请检查本地文件')
        return 1
    except KeyboardInterrupt:
        logging.info('监测已停止')
        return 0


if __name__ == '__main__':
    sys.exit(main())
