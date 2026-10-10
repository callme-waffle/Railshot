import copy
import json
import unittest
from unittest.mock import patch
import workload_diagnostics
from test_logs import LogsTest

class WorkloadDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.f = LogsTest(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.deployment['status'] = {'observedGeneration':1,'availableReplicas':1}
        c = self.f.pod['status']['containerStatuses'][0]
        c.update(ready=True,restartCount=0,state={'running':{}})
        self.f.pod['status']['conditions']=[{'type':'Ready','status':'True'}]

    def observe(self):
        with patch('argo.kubectl',side_effect=self.f.control), patch('credentials.customer',side_effect=self.f.customer):
            return workload_diagnostics.workload(self.f.config,self.f.review)

    def test_readiness_requires_exact_pod_owner_digest_and_observed_generation(self):
        self.assertEqual(self.observe()['state'],'ready')
        for change in ('owner','image','generation','error'):
            before=copy.deepcopy((self.f.pod,self.f.deployment))
            if change=='owner': self.f.pod['metadata']['ownerReferences'][0]['uid']='foreign'
            if change=='image': self.f.pod['status']['containerStatuses'][0]['imageID']='containerd://sha256:'+'f'*64
            if change=='generation': self.f.deployment['status']['observedGeneration']=0
            if change=='error': self.f.pod['status']['containerStatuses'][0].update(ready=False,state={'terminated':{'reason':'OOMKilled','exitCode':137,'message':'private-secret'}})
            result=self.observe()
            self.assertEqual(result['state'],'progressing')
            self.assertNotIn('private-secret',json.dumps(result))
            if change=='error': self.assertEqual(result['pods'][0]['containers'][0]['reason'],'OOMKilled')
            self.f.pod,self.f.deployment=before

    def test_transport_failure_is_unavailable_and_never_fabricates_readiness(self):
        with patch('logs.customer_auth',side_effect=ValueError('private-secret')):
            result=workload_diagnostics.workload(self.f.config,self.f.review)
        self.assertEqual(result['state'],'unavailable'); self.assertNotIn('private-secret',json.dumps(result))

if __name__=='__main__': unittest.main()
