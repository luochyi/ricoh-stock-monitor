import copy
import tempfile
import unittest
from pathlib import Path
import checkout as c

DETAIL={'storeInfo':{'id':42,'storeName':'官翻品 GR IIIx','goodsType':'normal','stock':1},
        'productValue':{'默认':{'productId':42,'unique':'sku42','stock':1,'price':6749}}}
ADDRESS={'id':3,'uid':2,'realName':'测试','phone':'13800000000','province':'测试省','city':'测试市','district':'测试区','detail':'测试地址'}
PREVIEW={'orderKey':'key42','addressInfo':ADDRESS,'priceGroup':{'totalPrice':6749,'storePostage':0},
 'cartInfo':[{'cartNum':1,'truePrice':6749,'productInfo':{'id':42,'storeName':'官翻品 GR IIIx','attrInfo':{'unique':'sku42'}}}]}

class FakeAPI:
    def __init__(self):
        self.detail=copy.deepcopy(DETAIL)
        self.preview=copy.deepcopy(PREVIEW)
        self.created=[]
        self.fail=False
    def call(self,path,body=None):
        if path.startswith('/product/detail'):return self.detail
        if path.startswith('/address/list'):return {'list':[copy.deepcopy(ADDRESS)]}
        if path=='/order/invoice/default':return {'id':5}
        if path=='/cart/add':return {'cartId':'cart42'}
        if path=='/order/confirm':return self.preview
        if path.startswith('/order/create/'):
            self.created.append(body)
            if self.fail:raise c.MonitorError('模拟创建响应超时')
            return {'result':{'orderId':'ORDER42'}}
        raise AssertionError(path)

class CheckoutTests(unittest.TestCase):
    def test_exact_target_and_stock(self):
        for name,stock,expected in [('官翻品 GR IV',1,False),('官翻品 GR IIIx',0,False),('官翻品 GR IIIx',1,True)]:
            self.assertEqual(bool(c.find_candidate({'code':0,'data':[{'id':42,'storeName':name,'stock':stock}]})),expected)
    def test_inconsistent_total_does_not_create(self):
        api=FakeAPI();api.preview['priceGroup']['totalPrice']=6750
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(c.MonitorError):c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},Path(d)/'s.json')
            self.assertEqual(api.created,[])
    def test_wrong_sku_or_address_does_not_create(self):
        for field in ['sku','address','qty']:
            api=FakeAPI()
            if field=='sku':api.preview['cartInfo'][0]['productInfo']['attrInfo']['unique']='other'
            if field=='address':api.preview['addressInfo']['id']=9
            if field=='qty':api.preview['cartInfo'][0]['cartNum']=2
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(c.MonitorError):c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},Path(d)/'s.json')
                self.assertEqual(api.created,[])
    def test_only_one_order_across_restarts(self):
        api=FakeAPI()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'s.json'
            c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},path)
            c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},path)
            self.assertEqual(len(api.created),1)
            self.assertEqual(api.created[0]['addressId'],3)
            self.assertEqual(api.created[0]['invoiceTitleId'],5)
    def test_ambiguous_create_is_never_retried(self):
        api=FakeAPI();api.fail=True
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'s.json'
            with self.assertRaises(c.MonitorError):c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},path)
            c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},path)
            self.assertEqual(len(api.created),1)
    def test_detail_stock_and_price_checked(self):
        for key,value in [('stock',0),('price',0),('price',-1),('price','NaN')]:
            api=FakeAPI();api.detail['productValue']['默认'][key]=value
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(c.MonitorError):c.attempt(api,{'id':42,'storeName':c.TARGET,'stock':1},Path(d)/'s.json')
                self.assertEqual(api.created,[])

if __name__=='__main__':unittest.main()
