import copy
import tempfile
import unittest
from pathlib import Path
import checkout as c
from test_checkout import FakeAPI


def product(name, product_id=42, stock=1):
    return {'id':product_id,'storeName':name,'stock':stock}


class PurchaseRulesTests(unittest.TestCase):
    def test_substring_alias_and_hdf_models_are_eligible(self):
        for name in ['官翻品 GR IIIx','理光 GR3x 官翻机','官翻 GR Ⅲx 黑色',
                     '官翻 GR IIIx HDF','官翻品 GR III HDF','官翻品 gr iii hdf 套装']:
            with self.subTest(name=name):
                self.assertEqual(c.find_candidate({'code':0,'data':[product(name)]})['storeName'],name)

    def test_unrelated_models_and_unavailable_products_are_not_selected(self):
        for name,stock in [('官翻品 GR III',1),('官翻品 GR IV',5),('官翻 GR III HDF',0)]:
            self.assertIsNone(c.find_candidate({'code':0,'data':[product(name,stock=stock)]}))

    def test_multiple_products_choose_one_iiix_first_then_stable_id(self):
        products=[product('官翻品 GR III HDF',1),product('官翻品 GR IIIx HDF',50),
                  product('官翻品 GR3x',42)]
        for rows in [products,list(reversed(products))]:
            self.assertEqual(c.find_candidate({'code':0,'data':rows})['id'],42)
        self.assertEqual(c.find_candidate({'code':0,'data':products[:2]})['id'],50)

    def test_duplicate_product_id_rejected(self):
        with self.assertRaises(c.MonitorError):
            c.find_candidate({'code':0,'data':[product(c.TARGET),product(c.TARGET)]})

    def test_expensive_iiix_or_hdf_can_create_only_one_order(self):
        for name in ['理光 GR3x 官翻相机','官翻品 GR III HDF']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                api=FakeAPI()
                api.detail['storeInfo']['storeName']=name
                api.detail['productValue']['默认']['price']=99999
                api.preview['cartInfo'][0]['productInfo']['storeName']=name
                api.preview['cartInfo'][0]['truePrice']=99999
                api.preview['priceGroup']={'totalPrice':100009,'storePostage':10}
                path=Path(directory)/'state.json'
                state=c.attempt(api,product(name),path)
                c.attempt(api,product(name),path)
                self.assertEqual(len(api.created),1)
                self.assertEqual(state['product_name'],name)
                self.assertEqual(state['total'],'100009')

    def test_different_eligible_name_in_detail_or_checkout_stops_order(self):
        for stage in ['detail','preview']:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                api=FakeAPI()
                if stage=='detail':api.detail['storeInfo']['storeName']='官翻品 GR III HDF'
                else:api.preview['cartInfo'][0]['productInfo']['storeName']='官翻品 GR III HDF'
                with self.assertRaises(c.MonitorError):
                    c.attempt(api,product(c.TARGET),Path(directory)/'state.json')
                self.assertEqual(api.created,[])

    def test_unmatched_candidate_cannot_bypass_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            api=FakeAPI()
            with self.assertRaises(c.MonitorError):
                c.attempt(api,product('官翻品 GR IV'),Path(directory)/'state.json')
            self.assertEqual(api.created,[])


if __name__=='__main__':unittest.main()
