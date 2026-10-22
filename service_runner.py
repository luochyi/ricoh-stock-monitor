"""macOS 后台服务入口：加载受限凭证、轮转日志并保留订单终止状态。"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def service_exit_code(child_code, root):
    # 库存监测始终恢复；购买状态由监测进程读取，已提交或待核查时不再下单。
    return 1


def main():
    root=Path(__file__).resolve().parent
    os.umask(0o077)
    logs=root/'logs'
    logs.mkdir(exist_ok=True)
    handler=RotatingFileHandler(logs/'monitor.log',maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger=logging.getLogger('service')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    child=None
    def stop(signum, frame):
        logger.info('服务收到停止信号 %s',signum)
        if child is not None and child.poll() is None:
            child.terminate()
        raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        values=json.loads((root/'private/dingtalk.json').read_text(encoding='utf-8'))
        env={**os.environ,**{key:values[key] for key in ('DINGTALK_WEBHOOK','DINGTALK_SECRET') if key in values},
             'PYTHONUNBUFFERED':'1','PYTHONIOENCODING':'utf-8'}
        command=['/usr/bin/caffeinate','-i',sys.executable,str(root/'watch_order.py'),
                 '--config',str(root/'config.ricoh.json'),'--credentials',str(root/'private/ricoh.json')]
        child=subprocess.Popen(command,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                               text=True,encoding='utf-8',errors='replace')
        logger.info('后台服务启动，监测进程 PID=%s',child.pid)
        for line in child.stdout:
            logger.info('%s',line.rstrip())
        code=child.wait()
        exit_code=service_exit_code(code,root)
        if exit_code:
            # 子进程退出后再写健康状态，避免与运行中的监测进程并发写入。
            from health import Health
            from monitor import send_dingtalk, MonitorError
            from operating_window import QuietHours
            os.environ.update({key:env[key] for key in ('DINGTALK_WEBHOOK','DINGTALK_SECRET') if key in env})
            try:
                health=Health(root/'health-state.json')
                health.failure(time.time(),'监测进程异常退出，macOS 将自动尝试恢复',immediate=True)
                health.flush(send_dingtalk,time.time())
            except (MonitorError,QuietHours,OSError,ValueError):
                logger.error('进程退出告警暂未送达，请检查健康状态与网络')
        logger.info('监测退出，原始状态=%s，服务状态=%s（1=系统稍后恢复库存监测）',code,exit_code)
        return exit_code
    except (OSError,ValueError,KeyError):
        logger.error('后台服务配置或文件操作失败，请检查本地配置和权限')
        return 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__=='__main__':
    raise SystemExit(main())
