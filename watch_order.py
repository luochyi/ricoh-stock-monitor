"""监测全部官翻品，为名称包含 GR IIIx／GR3x 或 HDF 的商品创建一笔待付款订单。"""
import argparse
import logging
from pathlib import Path
import time
import checkout
import monitor
from health import Health
from operating_window import active, QuietHours
from favorites import FavoritesWatch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(Path(__file__).with_name('config.ricoh.json')))
    parser.add_argument('--credentials',required=True,help='受限的本地登录凭证 JSON 路径')
    parser.add_argument('--preflight',action='store_true',help='仅核对登录、地址及默认发票')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config_path=Path(args.config).resolve()
    config=monitor.read_json(config_path)
    root=config_path.parent
    api=checkout.API(args.credentials)
    if args.preflight:
        checkout.preflight(api)
        logging.info('登录、第一条地址和默认发票检查通过；未创建订单')
        return 0
    if (config.get('purchase_rule') != checkout.PURCHASE_RULE
            or config['source'].get('mode') != 'ricoh_refurbished_list'
            or config['source'].get('scope') != 'all'):
        raise monitor.MonitorError('自动下单必须使用已授权的 IIIx 或 HDF 规则及全部官翻品数据源')
    monitor.webhook_config()
    order_path=root/'purchase-state.json'
    notice_path=root/'purchase-notices.json'
    stock_path=root/config['state_file']
    interval=config.get('interval_seconds',60)
    if type(interval) is not int or interval<30:
        raise monitor.MonitorError('监测间隔不得低于 30 秒')
    # 与通知进程使用相同库存锁，禁止同时运行两份监测进程。
    with monitor.exclusive_lock(stock_path):
        notices=monitor.read_json(notice_path) if notice_path.exists() else {'pending':[],'last_key':''}
        def enqueue(key,message):
            if notices['last_key']!=key:
                notices['pending'].append('理光库存提醒：'+message)
                notices['last_key']=key
                monitor.save_state(notice_path,notices)
        def flush():
            while notices['pending']:
                monitor.send_dingtalk(notices['pending'][0])
                notices['pending'].pop(0)
                monitor.save_state(notice_path,notices)
        health=Health(root/'health-state.json')
        favorite_settings=config.get('favorites',{})
        favorites=FavoritesWatch(api,root,favorite_settings,
            notification_policy=config.get('notification_policy',{})) if favorite_settings.get('enabled') else None
        if config.get('notification_policy',{}).get('mute_plain_gr4'):
            logging.info('库存通知：普通 GR4／GR IV 静默，HDF 及其他型号保留；列表和收藏共用规则')
        logging.info('开始监测全部官翻品；名称包含 %s 可自动下单，总共 1 台，不设价格上限，只创建待付款订单',checkout.PURCHASE_LABEL)
        logging.info('正常轮询等待间隔：%s 秒，接口查询失败时逐步退避',interval)
        logging.info('工作时段：北京时间 08:00–18:00；其余时间暂停查询、下单和全部通知')
        failures=0
        paused=False
        while True:
            if not active():
                if not paused:
                    health.pause(time.time())
                    logging.info('进入夜间暂停时段，等待次日 08:00 恢复')
                    paused=True
                failures=0
                time.sleep(30)
                continue
            if paused:
                logging.info('进入白天工作时段，恢复监测')
                paused=False
            stage='inventory'
            try:
                favorite_summary='';favorite_errors=[]
                if favorites is not None:
                    try:
                        favorite_summary,favorite_errors=favorites.cycle(monitor.send_dingtalk,time.time())
                    except monitor.MonitorError as exc:
                        favorite_errors=['收藏库存检查或通知失败：'+str(exc)]
                data=monitor.source_fetch(config)
                stage='notification'
                # 库存提醒不依赖购买登录，也不随一笔订单结束而停止。
                monitor.cycle(config,stock_path,lambda:data,monitor.send_dingtalk)
                stage='state'
                state=monitor.read_json(order_path) if order_path.exists() else {'phase':'ready'}
                if state.get('phase') not in ('ready','submitting','submitted'):
                    raise monitor.MonitorError('订单状态未知，禁止继续下单')
                candidate=checkout.find_candidate(data)
                if state['phase']=='ready':
                    stage='preflight'
                    checkout.preflight(api)
                    if candidate:
                        stage='checkout'
                        state=checkout.attempt(api,candidate,order_path)
                if state['phase']=='submitted':
                    enqueue('submitted',state.get('product_name',checkout.TARGET)+' 创建订单接口已返回成功，确认时含运费金额 ¥'+state['total']+'。请立即在理光小程序“我的 → 待付款”核对并付款。脚本不会自动支付或再次下单。')
                elif state['phase']=='submitting':
                    enqueue('uncertain','创建订单请求结果未确认，已禁止重复提交。请在小程序“待付款/全部订单”核查，勿直接删除 purchase-state.json 后重启。')
                flush()
                items=monitor.normalize(data,config['source'])
                summary='全部官翻品：当前列出 %s 款，有库存 %s 款。' % (
                    len(items),sum(item['stock']>0 for item in items.values()))
                if favorite_errors:
                    stage='favorites'
                    raise monitor.MonitorError('收藏库存监测异常：'+'；'.join(favorite_errors[:5]))
                if favorite_summary:
                    summary+='\n'+favorite_summary
                stage='notification'
                health.success(time.time(),bool(candidate),summary=summary,order_phase=state['phase'],interval=interval,
                               candidate_name=candidate['storeName'] if candidate else '')
                health.flush(monitor.send_dingtalk,time.time())
                failures=0
            except QuietHours:
                # 夜间边界中断正常工作，不生成故障告警；未发送通知保留在原队列。
                health.pause(time.time())
                continue
            except (monitor.MonitorError,KeyError,TypeError,ValueError) as exc:
                failures+=1
                reason=str(exc) if isinstance(exc,monitor.MonitorError) else '响应格式无法核实'
                logging.error('%s',reason)
                health.failure(time.time(),reason,immediate=stage in ('preflight','checkout','state'))
                try:
                    health.flush(monitor.send_dingtalk,time.time())
                    flush()
                except (monitor.MonitorError,QuietHours):
                    logging.error('钉钉通知失败，已保留待发消息')
            # 下单前置条件异常时仍保持库存检查频率；库存接口故障才退避。
            time.sleep(interval if stage in ('preflight','checkout','state','favorites') else
                       max(interval,min(interval*2**min(failures,5),1800)))


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except monitor.MonitorError as exc:
        logging.error('%s',exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        logging.info('自动下单监测已停止')
