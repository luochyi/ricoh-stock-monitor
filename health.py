"""持久化运行心跳：健康报告、故障告警与恢复补报。"""
from datetime import datetime
from pathlib import Path
from monitor import read_json, save_state, MonitorError
from checkout import PURCHASE_LABEL
from operating_window import BEIJING, active, same_session


def heartbeat_window(now):
    return active(now)


class Health:
    def __init__(self,path):
        self.path=Path(path)
        self.state=read_json(self.path) if self.path.exists() else {
            'version':1,'last_tick':None,'last_success':None,'last_report':None,
            'failures':0,'alerted':False,'failure_since':None,'pending':None}
        required=['last_tick','last_success','last_report','failures','alerted','pending','failure_since']
        if (not isinstance(self.state,dict) or self.state.get('version')!=1
                or any(k not in self.state for k in required)):
            raise MonitorError('健康状态文件异常，请检查 health-state.json')

    def save(self):
        save_state(self.path,self.state)

    def pause(self,now):
        # 计划内暂停清除过时的健康告警，不改库存、订单或最后成功时间。
        self.state.update(last_tick=now,failures=0,alerted=False,failure_since=None,pending=None)
        self.save()

    def success(self,now,available,summary='',order_phase='ready',interval=60,candidate_name=''):
        if not active(now):
            self.pause(now)
            return
        s=self.state
        gap=(s['failures']==0 and s['last_tick'] is not None
             and same_session(s['last_tick'],now) and now-s['last_tick']>300)
        recovering=s['alerted'] or gap
        s.update(last_tick=now,last_success=now,failures=0)
        stamp=datetime.fromtimestamp(now,BEIJING).strftime('%Y-%m-%d %H:%M:%S %z')
        stock='有货' if available else '无货'
        purchase=('下单登录、第一条地址和默认发票检查通过。' if order_phase=='ready' else
                  '已停止自动下单（%s），全部官翻品库存监测继续。' %
                  ('订单已提交' if order_phase=='submitted' else '订单结果待人工核查'))
        details=((summary+'\n' if summary else '')+'符合自动购买条件的商品：'+stock+'\n'
                 +('优先候选：'+candidate_name+'\n' if candidate_name else '')+purchase+'\n')
        if recovering:
            reason='检测到运行中断，现已恢复' if gap else '故障已恢复'
            s['pending']={'kind':'recovery','message':
                '理光库存提醒｜'+reason+'\n库存接口检查通过。\n'+details+
                '检查时间：'+stamp+'\n断网或停机期间无法即时发送，现补报状态。'}
        elif s.get('pending') and s['pending']['kind']=='recovery':
            pass
        elif heartbeat_window(now) and (s['last_report'] is None or now-s['last_report']>=3600):
            s['pending']={'kind':'heartbeat','message':
                ('理光库存提醒｜运行正常\n后台监测正在运行，正常每轮结束后等待 %s 秒再检查库存。\n' % interval)
                +details+'名称包含 '+PURCHASE_LABEL+' 可自动购买：总共 1 台，不设价格上限，只提交待付款订单。\n检查时间：'+stamp}
        elif s.get('pending') and s['pending']['kind']=='heartbeat':
            # 刷新尚未送达的健康报告，不堆积旧状态。
            s['pending']=None
        s['alerted']=False
        s['failure_since']=None
        self.save()

    def failure(self,now,reason,immediate=False):
        if not active(now):
            self.pause(now)
            return
        s=self.state
        s['last_tick']=now
        s['failures']+=1
        if s['failure_since'] is None:s['failure_since']=now
        if s.get('pending') and s['pending']['kind']!='fault':s['pending']=None
        if (immediate or s['failures']>=3) and not s['alerted']:
            s['alerted']=True
            s['pending']={'kind':'fault','message':
                '理光库存提醒｜监测故障\n'+reason+'\n本轮无法确认监测和下单条件正常。'
                '\n程序会继续检查，恢复后再次通知；创建订单结果不明时不会重复提交。'}
        self.save()

    def flush(self,send,now):
        pending=self.state.get('pending')
        if not pending:return
        if not active(now):
            # 所有健康消息在夜间静默，次日根据实时检查生成新报告。
            self.pause(now)
            return
        send(pending['message'])
        if pending['kind'] in ('heartbeat','recovery'):
            self.state['last_report']=now
        self.state['pending']=None
        self.save()
