"""按收藏商品 ID 查询规格库存，未上架商品也监测；只发送通知。"""
import logging
from pathlib import Path
import monitor


class FavoritesWatch:
    def __init__(self,api,root,settings,notification_policy=None):
        self.api=api
        self.root=Path(root)
        self.cache_path=self.root/'favorite-products.json'
        self.state_path=self.root/'favorite-stock-state.json'
        self.pinned=settings.get('pinned_product_ids',[])
        self.sync_seconds=settings.get('sync_seconds',300)
        if (not isinstance(self.pinned,list)
                or any(type(value) is not int or value<=0 for value in self.pinned)
                or type(self.sync_seconds) is not int or self.sync_seconds<60):
            raise monitor.MonitorError('收藏监测配置无效')
        self.cache=monitor.read_json(self.cache_path) if self.cache_path.exists() else {
            'version':1,'synced_at':None,'products':{}}
        if (not isinstance(self.cache,dict) or self.cache.get('version')!=1
                or not isinstance(self.cache.get('products'),dict)
                or any(not key.isdigit() or int(key)<=0 or not isinstance(value,str)
                       for key,value in self.cache['products'].items())):
            raise monitor.MonitorError('收藏商品缓存异常，请保留原文件并检查')
        self.config={'notify_initial_available':True,'source':{
            'success':{'path':'code','equals':0},'items_path':'data','allow_empty':True,
            'fields':{'id':'id','name':'name','stock':'stock'}}}
        self.config['notification_policy']=notification_policy or {}

    def sync(self,now):
        products={};seen=set()
        for page in range(1,101):
            data=self.api.call('/relation/collect/user?couponId=&keyword=&limit=10&news=&page='+
                               str(page)+'&sid=&type=collect')
            rows=data.get('list') if isinstance(data,dict) else data
            if not isinstance(rows,list):
                raise monitor.MonitorError('收藏列表结构异常，保留已知商品')
            if not rows:
                self.cache={'version':1,'synced_at':now,'products':products}
                monitor.save_state(self.cache_path,self.cache)
                logging.info('收藏同步成功：%s 款官翻商品',len(products))
                return
            for item in rows:
                if not isinstance(item,dict):
                    raise monitor.MonitorError('收藏商品结构异常')
                product_id=item.get('productId');name=item.get('storeName')
                # 收藏记录 ID 不是商品 ID，必须使用 productId。
                if (type(product_id) is not int or product_id<=0 or product_id in seen
                        or not isinstance(name,str) or not name.strip()):
                    raise monitor.MonitorError('收藏商品 ID、名称或分页重复，保留上次列表')
                seen.add(product_id)
                if '官翻' in name:
                    products[str(product_id)]=name
        raise monitor.MonitorError('收藏分页未结束，拒绝替换完整列表')

    def detail_rows(self,product_id):
        data=self.api.call('/product/detail?productId='+str(product_id))
        if not isinstance(data,dict) or not isinstance(data.get('storeInfo'),dict):
            raise monitor.MonitorError('收藏商品详情缺失，保留旧库存')
        info=data['storeInfo'];name=info.get('storeName');listed=info.get('isShow')
        if (type(info.get('id')) is not int or info['id']!=product_id
                or not isinstance(name,str) or '官翻' not in name
                or type(listed) is not int or listed not in (0,1)):
            raise monitor.MonitorError('商品身份、官翻标识或上架状态无法核实')
        values=data.get('productValue')
        if not isinstance(values,dict) or not values:
            raise monitor.MonitorError('规格库存列表缺失，保留旧库存')
        rows=[];seen=set()
        for label,sku in values.items():
            if not isinstance(sku,dict):
                raise monitor.MonitorError('规格字段无效')
            unique=sku.get('unique');stock=sku.get('stock')
            if (type(sku.get('productId')) is not int or sku['productId']!=product_id
                    or not isinstance(unique,str) or not unique or unique in seen
                    or type(stock) is not int or stock<0):
                raise monitor.MonitorError('规格库存或唯一标识无效，保留旧库存')
            seen.add(unique)
            status='已上架' if listed else '未上架，不代表可下单'
            rows.append({'id':str(product_id)+':'+unique,'stock':stock,
                         'name':'[收藏/关注] '+name+'（规格：'+str(label)+'；'+status+'）'})
        return rows

    def cycle(self,send,now):
        errors=[]
        synced=self.cache.get('synced_at')
        if synced is None or now-synced>=self.sync_seconds:
            try:
                self.sync(now)
            except monitor.MonitorError as exc:
                errors.append('收藏同步失败：'+str(exc))
        watched={int(key) for key in self.cache['products']}|set(self.pinned)
        rows=[];succeeded=0;available=0
        for product_id in sorted(watched):
            try:
                product_rows=self.detail_rows(product_id)
                rows.extend(product_rows);succeeded+=1
                available+=int(any(row['stock']>0 for row in product_rows))
            except monitor.MonitorError as exc:
                errors.append('商品 '+str(product_id)+'：'+str(exc))
        # 单个详情失败不阻断其他商品，且不会把未知库存改写为零。
        if succeeded or not errors:
            monitor.cycle(self.config,self.state_path,lambda:{'code':0,'data':rows},send)
        summary=('收藏/关注详情：已检查 %s/%s 款、%s 个规格，%s 款有规格库存（不代表已上架）。' %
                 (succeeded,len(watched),len(rows),available))
        logging.info('%s',summary)
        return summary,errors
