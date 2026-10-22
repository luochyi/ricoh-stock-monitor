import copy
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import monitor
import favorites


class FavoriteAPI:
    def __init__(self):
        self.calls=[];self.broken=set();self.pages={1:[
            {'id':999,'productId':130,'storeName':'官翻品 GR IV'}],2:[]}
        self.details={130:{'storeInfo':{'id':130,'storeName':'官翻品 GR IV','stock':0,'isShow':0},
            'productValue':{'默认':{'productId':130,'unique':'sku130','stock':12,'sku':'默认'}}}}
    def call(self,path,body=None):
        self.calls.append(path)
        if path.startswith('/relation/collect/user?'):
            if 'list' in self.broken:raise monitor.MonitorError('收藏登录失效')
            return self.pages[int(parse_qs(urlsplit(path).query)['page'][0])]
        product_id=int(parse_qs(urlsplit(path).query)['productId'][0])
        if product_id in self.broken:raise monitor.MonitorError('商品详情暂时不可用')
        return copy.deepcopy(self.details[product_id])


class FavoritesTests(unittest.TestCase):
    def test_new_favorite_is_discovered_on_next_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FavoriteAPI();sent=[];watch=favorites.FavoritesWatch(api,Path(directory),{})
            watch.cycle(sent.append,1000)
            api.details[65]=copy.deepcopy(api.details[130])
            api.details[65]['storeInfo'].update(id=65,storeName='官翻品 GR IIIx')
            api.details[65]['productValue']['默认'].update(productId=65,unique='sku65',stock=1)
            api.pages[1].append({'productId':65,'storeName':'官翻品 GR IIIx'})
            watch.cycle(sent.append,1300)
            self.assertEqual(len(sent),2);self.assertIn('GR IIIx',sent[-1])

    def test_hidden_product_uses_sku_stock_and_not_collection_record_id(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FavoriteAPI();sent=[]
            watch=favorites.FavoritesWatch(api,Path(directory),{})
            summary,errors=watch.cycle(sent.append,1000)
            self.assertEqual(errors,[]);self.assertEqual(len(sent),1)
            self.assertIn('12',sent[0]);self.assertIn('未上架',sent[0]);self.assertIn('GR IV',sent[0])
            self.assertIn('/product/detail?productId=130',api.calls)
            self.assertFalse(any('productId=999' in path for path in api.calls))
            self.assertIn('1 款',summary)

    def test_stock_change_notifies_once_across_restart_and_zero_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FavoriteAPI();sent=[];root=Path(directory)
            watch=favorites.FavoritesWatch(api,root,{})
            watch.cycle(sent.append,1000)
            watch=favorites.FavoritesWatch(api,root,{})
            watch.cycle(sent.append,1030);self.assertEqual(len(sent),1)
            api.details[130]['productValue']['默认']['stock']=13
            watch.cycle(sent.append,1060);self.assertEqual(len(sent),2)
            self.assertIn('12 → 13',sent[-1])
            api.details[130]['productValue']['默认']['stock']=0
            watch.cycle(sent.append,1090);self.assertIn('售罄',sent[-1])

    def test_sync_pagination_filter_and_periodic_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FavoriteAPI();api.pages={1:[{'productId':5,'storeName':'全新 GR IIIx'}],
                2:[{'productId':130,'storeName':'官翻品 GR IV'}],3:[]}
            watch=favorites.FavoritesWatch(api,Path(directory),{})
            watch.cycle(lambda message:None,1000)
            self.assertEqual(sum('collect/user' in path for path in api.calls),3)
            watch.cycle(lambda message:None,1030)
            self.assertEqual(sum('collect/user' in path for path in api.calls),3)
            api.pages={1:[]}
            watch.cycle(lambda message:None,1300)
            self.assertEqual(sum('collect/user' in path for path in api.calls),4)

    def test_sync_failure_keeps_known_products_and_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FavoriteAPI();sent=[];watch=favorites.FavoritesWatch(api,Path(directory),{})
            watch.cycle(sent.append,1000);api.broken.add('list')
            api.details[130]['productValue']['默认']['stock']=14
            _,errors=watch.cycle(sent.append,1300)
            self.assertTrue(errors);self.assertIn('12 → 14',sent[-1])

    def test_failed_detail_preserves_stock_while_other_product_notifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);api=FavoriteAPI();sent=[]
            api.details[65]=copy.deepcopy(api.details[130])
            api.details[65]['storeInfo'].update(id=65,storeName='官翻品 GR IIIx')
            api.details[65]['productValue']['默认'].update(productId=65,unique='sku65',stock=0)
            watch=favorites.FavoritesWatch(api,root,{'pinned_product_ids':[65]})
            watch.cycle(sent.append,1000);api.broken.add(130)
            api.details[65]['productValue']['默认']['stock']=1
            _,errors=watch.cycle(sent.append,1030)
            self.assertTrue(errors);self.assertIn('GR IIIx',sent[-1])
            self.assertEqual(monitor.read_json(root/'favorite-stock-state.json')['items']['130:sku130']['stock'],12)

    def test_bad_sku_data_does_not_become_zero(self):
        for mutation in ['missing','negative','wrong_product','duplicate']:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);api=FavoriteAPI();sent=[]
                watch=favorites.FavoritesWatch(api,root,{})
                watch.cycle(sent.append,1000)
                values=api.details[130]['productValue']
                if mutation=='missing':values['默认'].pop('stock')
                if mutation=='negative':values['默认']['stock']=-1
                if mutation=='wrong_product':values['默认']['productId']=999
                if mutation=='duplicate':values['其他']=copy.deepcopy(values['默认'])
                _,errors=watch.cycle(sent.append,1030)
                self.assertTrue(errors);self.assertEqual(len(sent),1)
                self.assertEqual(monitor.read_json(root/'favorite-stock-state.json')['items']['130:sku130']['stock'],12)

    def test_send_failure_retains_message_and_retry_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            watch=favorites.FavoritesWatch(FavoriteAPI(),Path(directory),{})
            def fail(message):raise monitor.MonitorError('通知发送失败')
            with self.assertRaises(monitor.MonitorError):watch.cycle(fail,1000)
            sent=[];watch.cycle(sent.append,1030);watch.cycle(sent.append,1060)
            self.assertEqual(len(sent),1)


if __name__=='__main__':unittest.main()
