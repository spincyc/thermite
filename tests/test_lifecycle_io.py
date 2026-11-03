"""Bounded per-device execution, immutable admission and durable uncertainty."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

AREA=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(AREA/'lib'))
from oh_conf import House, Conf, YamlFile  # noqa: E402
from oh_features.routing import lifecycle_io  # noqa: E402
from oh_integrations import ukm404_http, avpro_telnet, sony_adcp, sony_ip_control, pyatv_http, sony_rest  # noqa: E402
from oh_testing import routing as routing_check  # noqa: E402


class Rendering(unittest.TestCase):
    def inventory(self):
        return {"version":1,"rooms":{"desk":{"label":"Desk","tag":"Room"}},"devices":[
            {"id":"desk_usb","room":"desk","kind":"usb_matrix","ip":"192.0.2.10",
             "controls":{"control":["ukm404_http"]}}]}

    def test_actor_uses_native_thing_configuration_not_duplicate_addresses(self):
        data=self.inventory();house=House(data);conf=Conf();out=conf.yaml_file(ukm404_http.FILE,())
        ukm404_http.render(data['devices'][0],house,out)
        cap=lifecycle_io.capabilities(data)
        self.assertTrue(cap['desk_usb_device1_host']['terminal'])
        descriptor={'version':1,'resources':{name:{} for name in cap}}
        lifecycle_io.configure(house,conf,descriptor)
        actor=conf.files['automation/js/thermite_routing_io_desk_usb.js']
        self.assertNotIn('192.0.2.10',actor)
        self.assertEqual(actor.count('const SEND = true;'),1)
        self.assertIn(lifecycle_io.MODULE,conf.files)
        self.assertEqual(descriptor['io']['status'],lifecycle_io.STATUS_ITEM)
        self.assertTrue(conf.yaml[lifecycle_io.ITEMS].items[lifecycle_io.STATUS_ITEM]['metadata']['stateDescription']['config']['readOnly'])
        self.assertEqual(conf.executor_controls,set(cap))
        for item in out.items.values():
            self.assertNotIn('channel',item)
            self.assertEqual(next(iter(item['channels'].values()))['toItemScript'],'|input')
        self.assertTrue(all(c['config']['mode']=='READONLY' for t in out.things.values() for c in t['channels'].values()))

    def test_unsupported_resources_are_rejected_before_rendering(self):
        with self.assertRaisesRegex(ValueError,'no ordered executor'):
            lifecycle_io.configure(House(self.inventory()),Conf(),{'resources':{'unknown_power':{}}})

    def test_projected_ownership_keeps_only_selected_claims_and_all_explicit_actor_controls(self):
        data=self.inventory();house=House(data);conf=Conf();ukm404_http.render(data['devices'][0],house,conf.yaml_file(ukm404_http.FILE,()))
        item='desk_usb_device1_host';descriptor={'resources':{item:{}}}
        lifecycle_io.configure(house,conf,descriptor)
        actor=descriptor['io']['actors']['desk_usb']
        self.assertEqual(actor['resources'],[item])
        self.assertEqual(set(actor['fields']),set(lifecycle_io.capabilities(data)))
        self.assertEqual(conf.executor_controls,set(actor['fields']))
        self.assertEqual(descriptor['io']['status'],lifecycle_io.STATUS_ITEM)
        module=conf.files[lifecycle_io.MODULE]
        self.assertEqual(module.count('const SEND = true;'),1)

    def test_custom_actor_reuses_its_device_configuration_and_command_context(self):
        for module,kind in ((pyatv_http,'streamer'),(sony_ip_control,'tv'),(sony_adcp,'projector'),(avpro_telnet,'hdmi_matrix')):
            with self.subTest(module=module.NAME):
                data=self.inventory();device=data['devices'][0]
                device.update(id='desk_device',kind=kind,make='Example',model='Test',controls={'control':[module.NAME]})
                house=House(data);conf=Conf();out=conf.yaml_file(module.FILE,())
                module.render(device,house,out)
                if module is sony_ip_control:
                    sony_rest.render(device,house,conf.yaml_file(sony_rest.FILE,()))
                conf.files.update(module.files([device],house))
                capabilities=lifecycle_io.capabilities(data)
                self.assertTrue(capabilities)
                self.assertTrue(all(cap['terminal'] for cap in capabilities.values()))
                lifecycle_io.configure(house,conf,{'resources':{item:{} for item in capabilities}})
                path=module.rule_path(device)
                text=conf.files[path]
                self.assertNotIn('const ROUTING_EXECUTOR = null;',text)
                self.assertEqual(text.count('const SEND = true;'),1)
                self.assertIn('routingLifecycleActor.legacy(',text)
                # The actor receives operational metadata, never another IP.
                config_line=next(line for line in text.splitlines() if line.startswith('const ROUTING_EXECUTOR = '))
                self.assertNotIn(device['ip'],config_line)
                aimed=module.aim(conf.files,device,'127.0.0.1:40123')
                self.assertNotEqual(aimed[path],text)
                self.assertEqual(next(line for line in aimed[path].splitlines() if line.startswith('const ROUTING_EXECUTOR = ')),config_line)


@unittest.skipUnless(shutil.which('node'),'node required')
class Broker(unittest.TestCase):
    def test_actual_executor_source(self):
        result=subprocess.run([shutil.which('node'),str(AREA/'tests/lifecycle_io_harness.js'),str(lifecycle_io.SOURCE),str(pyatv_http.SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['passed'],29)


class Quiescence(unittest.TestCase):
    def test_activity_snapshot_is_bounded_and_contains_only_nonnegative_counts(self):
        idle=dict(pending=0,active=0,quarantined=0,eventsQueued=0)
        self.assertEqual(routing_check.executor_activity(json.dumps(idle)),idle)
        bad=[None,'UNDEF','not JSON','{}','[]',' '*1025]
        bad += [json.dumps({**idle,**change}) for change in ({'active':True},{'pending':-1},{'active':1.5},{'extra':'ticket'})]
        for value in bad:
            with self.subTest(value=value):self.assertIsNone(routing_check.executor_activity(value))

    def test_pending_write_retires_before_silent_fixture_is_muted(self):
        check=object.__new__(routing_check.Check);check.executor_enabled=True;check.failures=[];check.log=lambda _:None
        check.routing={'hdmi_matrix':'matrix'};check.switches=[]
        class Matrix:
            muted=False
        matrix=Matrix();check.sims={'matrix':matrix};timeline=[];clock=[0.0];reads=[0]
        def states():
            self.assertFalse(matrix.muted,'the fixture was muted before the queued write retired')
            reads[0]+=1
            return {lifecycle_io.STATUS_ITEM:json.dumps(dict(pending=int(reads[0]==1),active=0,quarantined=0,eventsQueued=0))}
        check.states=states;check._writes=lambda _:0;check.sources=lambda _:None;check.readbacks=lambda *args:{}
        def answering(*args):timeline.append(matrix.muted)
        check.answering=answering
        def sleep(duration):clock[0]+=duration
        with mock.patch.object(routing_check.time,'monotonic',side_effect=lambda:clock[0]),mock.patch.object(routing_check.time,'sleep',side_effect=sleep):
            check.step_silent()
        self.assertEqual(timeline,[True,False]);self.assertGreaterEqual(reads[0],3);self.assertEqual(check.failures,[])

    def test_quarantined_executor_never_mutes_the_fixture(self):
        check=object.__new__(routing_check.Check);check.executor_enabled=True;check.failures=[];check.log=lambda _:None
        check.states=lambda:{lifecycle_io.STATUS_ITEM:json.dumps(dict(pending=0,active=0,quarantined=1,eventsQueued=0))}
        self.assertFalse(check.executor_idle('unsafe fixture change',timeout=0))
        self.assertEqual(len(check.failures),1)


@unittest.skipUnless(all(os.environ.get(name) for name in ('THERMITE_JAVA','THERMITE_ECJ','THERMITE_RUNTIME','THERMITE_GRAAL')),
                     'native broker test needs cached pinned Java/runtime/ECJ/Graal paths')
class NativeBroker(unittest.TestCase):
    def test_real_java_shared_broker_independent_contexts_and_journal(self):
        jars=sorted(Path(os.environ['THERMITE_RUNTIME']).rglob('*.jar'))+sorted(Path(os.environ['THERMITE_GRAAL']).rglob('*.jar'))
        classpath=os.pathsep.join(map(str,jars));java=os.environ['THERMITE_JAVA']
        scratch=AREA.parents[1]/'.scratch/lifecycle_impl/executor'
        scratch.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            directory=Path(temporary);(directory/'userdata').mkdir()
            compiled=subprocess.run([java,'-jar',os.environ['THERMITE_ECJ'],'-21','-proc:none','-cp',classpath,'-d',temporary,
                                     str(AREA/'tests/lifecycle_io/NativeLifecycleTest.java')],capture_output=True,text=True,timeout=30)
            self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
            try:
                result=subprocess.run([java,'-cp',temporary+os.pathsep+classpath,'NativeLifecycleTest',str(lifecycle_io.SOURCE),
                                       str(AREA/'lib/oh_integrations/_io.js'),str(directory/'userdata'),str(sony_ip_control.SOURCE)],capture_output=True,text=True,timeout=35)
            except subprocess.TimeoutExpired as error:
                self.fail('native broker timeout: '+str(error.stdout)+' '+str(error.stderr))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('native broker isolation, actor concurrency, durable uncertainty and reload fencing passed',result.stdout)
