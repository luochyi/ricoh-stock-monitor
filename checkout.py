"""按已授权条件提交一笔待付款订单；不会调用支付接口。"""
import hashlib
import re
import secrets
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode, quote
from monitor import MonitorError, request_json, read_json, save_state
from operating_window import QuietHours

TARGET = '官翻品 GR IIIx'
PURCHASE_RULE = 'gr_iiix_or_hdf'
PURCHASE_LABEL = 'GR IIIx／GR3x 或 HDF'


def name_key(name):
    # 兼容空格、大小写、全角字符及 Unicode 罗马数字。
    if not isinstance(name,str):
        return ''
    return re.sub(r'[\s_-]+','',unicodedata.normalize('NFKC',name).casefold())


def is_iiix(name):
    key=name_key(name)
    return 'griiix' in key or 'gr3x' in key


def matches_name(name):
    return is_iiix(name) or 'hdf' in name_key(name)


def money(value):
    if isinstance(value, bool) or value is None:
        raise MonitorError('订单金额字段无效')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise MonitorError('订单金额格式无效') from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
        raise MonitorError('订单金额精度或范围无效')
    return amount


class API:
    def __init__(self, private_path):
        self.private_path = private_path

    def call(self, path, body=None):
        # 仅允许经过核实的只读、结算与创建订单接口。
        allowed = ['/product/detail?', '/relation/collect/user?', '/address/list?', '/order/invoice/default',
                   '/cart/add', '/order/confirm', '/order/create/']
        if not any(path.startswith(prefix) for prefix in allowed):
            raise MonitorError('订单适配器不允许该接口')
        private = read_json(self.private_path)
        token = private.get('authorization', '')
        if not token.startswith('Bearer '):
            raise MonitorError('登录凭证不可用，请在电脑小程序重新登录')
        headers = {'Authorization': token}
        if path.startswith(('/product/detail?', '/relation/collect/user?')):
            endpoint, query = path.split('?', 1)
            stamp = str(int(time.time() * 1000))
            nonce = stamp + secrets.token_hex(8)
            digest = hashlib.sha256('\n'.join(['miniapp-product-sign', 'GET', endpoint,
                query, '', stamp, nonce]).encode('utf-8')).hexdigest()
            headers.update({'X-Request-Timestamp':stamp,'X-Request-Nonce':nonce,'X-Request-Sign':digest})
        result = request_json('https://shop.ricn-mall.com/app-api'+path,
                              'GET' if body is None else 'POST', body, headers)
        if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
            code = result.get('code') if isinstance(result, dict) else 'unknown'
            raise MonitorError('订单接口未成功（代码 %s），请检查登录、库存或账户条件' % code)
        return result.get('data')


def find_candidate(response):
    if (not isinstance(response, dict) or type(response.get('code')) is not int
            or response['code'] != 0 or not isinstance(response.get('data'), list)):
        raise MonitorError('库存响应异常，不执行下单')
    matches = []
    for item in response['data']:
        if not isinstance(item, dict):
            raise MonitorError('商品列表格式异常')
        if matches_name(item.get('storeName')):
            stock = item.get('stock')
            if type(stock) is not int or stock < 0:
                raise MonitorError('目标型号库存字段异常')
            if stock > 0:
                if type(item.get('id')) is not int or item['id'] <= 0:
                    raise MonitorError('商品 ID 无效')
                matches.append(item)
    if len({item['id'] for item in matches}) != len(matches):
        raise MonitorError('候选商品 ID 重复，无法唯一确定购买对象')
    # 多个匹配商品仅选一台：优先 IIIx，同优先级按 ID 排序，避免列表顺序波动。
    return min(matches,key=lambda item:(not is_iiix(item['storeName']),item['id'])) if matches else None


def first_address(api):
    data = api.call('/address/list?pageNo=1&pageSize=10')
    rows = data.get('list') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise MonitorError('地址列表为空或异常，不能选择第一条地址')
    first = rows[0]
    if not isinstance(first, dict) or any(not first.get(k) for k in ['id','realName','phone','province','city','detail']):
        raise MonitorError('第一条地址信息不完整')
    return first


def invoice(api):
    title = api.call('/order/invoice/default')
    if not isinstance(title, dict) or not title.get('id'):
        raise MonitorError('尚未设置默认发票抬头，自动下单暂不可用')
    return title


def preflight(api):
    address, title = first_address(api), invoice(api)
    return {'address_id':address['id'], 'invoice_id':title['id']}


def select_sku(detail, product_id, product_name):
    store = detail.get('storeInfo', {})
    if (store.get('id') != product_id or not matches_name(store.get('storeName'))
            or store.get('storeName') != product_name or store.get('goodsType') != 'normal'):
        raise MonitorError('商品详情与授权的型号或商品类型不符')
    values = detail.get('productValue')
    if not isinstance(values, dict) or len(values) != 1:
        raise MonitorError('规格数量不是一个，请人工确认规格')
    sku = next(iter(values.values()))
    if not isinstance(sku, dict) or sku.get('productId') != product_id or not sku.get('unique'):
        raise MonitorError('规格标识无效')
    if type(sku.get('stock')) is not int or sku['stock'] < 1:
        raise MonitorError('详情规格库存不足')
    if money(sku.get('price')) <= 0:
        raise MonitorError('规格价格必须为有效正金额')
    return sku


def validate_preview(preview, product_id, sku, address, product_name):
    if not isinstance(preview, dict) or not preview.get('orderKey'):
        raise MonitorError('订单确认数据缺少订单键')
    selected = preview.get('addressInfo', {})
    for field in ['id','realName','phone','province','city','district','detail']:
        if selected.get(field) != address.get(field):
            raise MonitorError('结算地址与第一条地址不一致')
    rows = preview.get('cartInfo')
    if not isinstance(rows, list) or len(rows) != 1:
        raise MonitorError('结算商品数量不符合授权')
    row = rows[0]
    product = row.get('productInfo', {})
    if (type(row.get('cartNum')) is not int or row['cartNum'] != 1
            or product.get('id') != product_id or not matches_name(product.get('storeName'))
            or product.get('storeName') != product_name
            or product.get('attrInfo', {}).get('unique') != sku['unique']):
        raise MonitorError('结算型号、规格或购买数量不一致')
    group = preview.get('priceGroup', {})
    total = money(group.get('totalPrice'))
    postage = money(group.get('storePostage'))
    unit = money(row.get('truePrice'))
    if total <= 0 or unit <= 0 or total != unit + postage:
        raise MonitorError('结算金额无效或商品金额加运费与总额不一致')
    return total


def attempt(api, candidate, state_path):
    path = Path(state_path)
    state = read_json(path) if path.exists() else {'phase':'ready'}
    if state.get('phase') in ('submitting','submitted'):
        return state
    if state.get('phase') != 'ready':
        raise MonitorError('订单状态不明，停止自动提交')
    if (not isinstance(candidate,dict) or not matches_name(candidate.get('storeName'))
            or type(candidate.get('id')) is not int or candidate['id'] <= 0
            or type(candidate.get('stock')) is not int or candidate['stock'] <= 0):
        raise MonitorError('候选商品不符合授权名称或库存条件')
    product_id = candidate['id']
    product_name = candidate['storeName']
    detail = api.call('/product/detail?'+urlencode({'productId':product_id}))
    sku = select_sku(detail, product_id, product_name)
    address = first_address(api)
    title = invoice(api)
    cart = api.call('/cart/add', {'orderType':1,'cartNum':1,'productId':product_id,
                                  'uniqueId':sku['unique'],'new':1})
    if not isinstance(cart, dict) or not cart.get('cartId'):
        raise MonitorError('未取得本次购买记录 ID，不提交订单')
    preview = api.call('/order/confirm', {'cartId':cart['cartId'],'orderType':1,
                        'addressId':address['id'],'shippingType':1,'couponId':-1})
    total = validate_preview(preview, product_id, sku, address, product_name)
    # 提交前再次核对第一条地址，避免确认期间列表顺序或地址内容变化。
    if first_address(api) != address:
        raise MonitorError('地址列表第一条发生变化，本轮不提交')
    payload = {'key':preview['orderKey'],'addressId':address['id'],'bargainId':0,
               'combinationId':0,'couponId':-1,'from':'','mark':'','pinkId':0,
               'seckillId':0,'shippingType':1,'isChannel':1,'orderType':1,
               'invoiceTitleId':title['id']}
    # 先落盘提交标记：超时、断电、通知失败都不得重发创建请求。
    state = {'phase':'submitting','product_id':product_id,'product_name':product_name,'sku':sku['unique'],
             'order_key':preview['orderKey'],'total':str(total),'started_at':int(time.time())}
    save_state(path,state)
    try:
        result = api.call('/order/create/'+quote(str(preview['orderKey']),safe=''),payload)
    except QuietHours:
        # 时段异常仅在 HTTP 请求发出前抛出，已确认未提交；次日重新核对库存和结算。
        save_state(path,{'phase':'ready','deferred_reason':'quiet_hours_before_send'})
        raise
    state.update({'phase':'submitted','result':result})
    save_state(path,state)
    return state
