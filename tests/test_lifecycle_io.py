"""Bounded per-device execution, immutable admission and durable uncertainty."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

AREA=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(AREA/'lib'))
from oh_conf import House, Conf, YamlFile  # noqa: E402
from oh_features.routing import lifecycle_io  # noqa: E402
import oh_integrations as integrations  # noqa: E402
from oh_integrations import ukm404_http, avpro_telnet, sony_adcp, sony_ip_control, pyatv_http, sony_rest, denonmarantz_http  # noqa: E402
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

    def test_an_integration_settle_reaches_its_field_and_the_planner_resource(self):
        from tests.test_zone_sync import LOUNGE, device, house
        data=house()
        data['devices'].append(device('example_projector',LOUNGE,'projector',29,make='Sony',model='Example Projector',
                                      controls={'observe':[sony_adcp.NAME],'control':[sony_adcp.NAME]}))
        data['routing']['rooms'][LOUNGE]['display']={'device':'example_projector','input':'hdmi1'}
        cap=lifecycle_io.capabilities(data)
        self.assertEqual(cap['example_projector_power']['field']['settle'],{'ON':60000,'OFF':30000})
        self.assertNotIn('settle',cap['example_projector_input']['field'])
        from oh_features import routing
        resources=routing.table(data)['lifecycle']['resources']
        self.assertEqual(resources['example_projector_power']['settleMs'],{'ON':60000,'OFF':30000})
        # Only the fields that declare one: the projector's, and the Sony TV's boot.
        self.assertEqual(sorted(name for name,row in resources.items() if 'settleMs' in row),['example_projector_power','example_screen_power'])
        self.assertEqual(resources['example_screen_power']['settleMs'],{'ON':sony_ip_control.BOOT_MS})
        declared=sony_adcp.fields
        for bad in ({'STANDBY':1000},{'ON':0},{'ON':300001},{'ON':1.5},{'ON':True}):
            def fields(device,bad=bad):
                out=declared(device);out[device['id']+'_power']['settle']=bad;return out
            with self.subTest(settle=bad),mock.patch.object(sony_adcp,'fields',fields):
                with self.assertRaisesRegex(ValueError,'sony_adcp declares an invalid settle for example_projector_power'):
                    lifecycle_io.capabilities(data)

    def test_matrix_outputs_are_independent_from_field_to_planner_resource(self):
        from tests.test_zone_sync import house
        from oh_integrations import bk808_http
        data=house()
        cap=lifecycle_io.capabilities(data)
        matrices={'example_matrix','example_usb'}
        for item,row in cap.items():
            with self.subTest(item=item):
                self.assertIs(row['field'].get('independent'),True if row['key'] in matrices else None)
        self.assertTrue(any(row['key']=='example_matrix' and item.endswith('_edid') for item,row in cap.items()))
        avpro={'id':'example_avpro','kind':'hdmi_matrix'}
        self.assertTrue(all(field['independent'] is True for field in avpro_telnet.fields(avpro).values()))
        from oh_features import routing
        resources=routing.table(data)['lifecycle']['resources']
        independent=sorted(name for name,row in resources.items() if 'independent' in row)
        self.assertTrue(independent)
        self.assertEqual(independent,sorted(name for name,row in resources.items() if row['key'] in matrices))
        self.assertTrue(all(resources[name]['independent'] is True for name in independent))
        declared=bk808_http.fields
        for bad in ('yes',1,None):
            def fields(device,bad=bad):
                out=declared(device)
                for field in out.values(): field['independent']=bad
                return out
            with self.subTest(independent=bad),mock.patch.object(bk808_http,'fields',fields):
                with self.assertRaisesRegex(ValueError,'bk808_http declares an invalid independent for example_matrix_'):
                    lifecycle_io.capabilities(data)

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


#: The routing core, which reads only the integrations' declarations.
GENERIC = ('lib/oh_features/routing/lifecycle_io.py', 'lib/oh_features/routing/lifecycle_io.js',
           'lib/oh_features/routing/lifecycle.py', 'lib/oh_features/routing/lifecycle.js',
           'lib/oh_features/routing/routing.js', 'lib/oh_room_policy/plan.py')


def fictional(kind='http'):
    """An integration no generic file knows: a relay whose one field is
    driven by the generic HTTP actor, or (any other kind) by its own rule."""
    module = types.ModuleType('example_relay_http')
    module.NAME, module.PHASE, module.FILE = 'example_relay_http', 'control', 'yaml/example_relay.yaml'
    uid = lambda device: f"http:url:{device['id']}"
    def render(device, house, out):
        out.things[uid(device)] = {'config': {'baseURL': f"http://{device['ip']}"}, 'channels': {'relay': {'config': {}}}}
        out.items[f"{device['id']}_relay"] = {'type': 'Switch', 'tags': ['Control'], 'channel': f"{uid(device)}:relay"}
    def fields(device):
        declared = {'kind': kind, 'values': ['ON', 'OFF'], 'settle': {'ON': 5000}, 'manual': 4}
        if kind == 'http':
            declared = {'kind': kind, 'thing': uid(device), 'channel': 'relay', **declared, 'ack': 'result'}
        return {f"{device['id']}_relay": declared}
    module.render, module.fields = render, fields
    if kind != 'http':
        module.rule_path = lambda device: f"automation/js/example_relay_{device['id']}.js"
        module.files = lambda devices, house: {module.rule_path(d): "const SEND = true;\n" + lifecycle_io.CUSTOM_MARK + "\n" for d in devices}
    return module


class Declarations(unittest.TestCase):
    def inventory(self):
        return {"version":1,"rooms":{"desk":{"label":"Desk","tag":"Room"}},"devices":[
            {"id":"desk_relay","room":"desk","kind":"relay","ip":"192.0.2.20","controls":{"control":["example_relay_http"]}}]}

    def test_the_generic_routing_core_names_no_integration(self):
        vendors = ('denon', 'marantz', 'pyatv', 'apple', 'sony', 'bravia', 'adcp', 'avpro', 'vrroom', 'hdfury', 'bk808', 'ukm404', 'orei')
        for path in GENERIC:
            text = (AREA / path).read_text(encoding='utf-8')
            for name in sorted(module.NAME for module in integrations.REGISTRY):
                with self.subTest(path=path, name=name):
                    self.assertIsNone(re.search(rf'\b{name}\b', text))
            for vendor in vendors:
                with self.subTest(path=path, vendor=vendor):
                    self.assertIsNone(re.search(rf'\b{vendor}', text, re.I))
            # Vendor values: Denon's zone source and game alias, an Item-name
            # suffix taken for power, the Sony inputs a diagnosis forwarded,
            # one matrix's EDID codes.
            for literal in ('"SOURCE"', "'SOURCE'", "'GAME", '"GAME', 'endswith("_power")', "endswith('_power')", 'hdmi\\d', 'range(1, 48)'):
                with self.subTest(path=path, literal=literal):
                    self.assertFalse(literal in text, literal)

    def test_a_new_integration_gets_an_executor_field_from_its_declaration_alone(self):
        module = fictional()
        data = self.inventory()
        with mock.patch.object(integrations, 'REGISTRY', (*integrations.REGISTRY, module)):
            caps = lifecycle_io.capabilities(data)
            self.assertEqual(caps, {'desk_relay_relay': {
                'key': 'desk_relay', 'provider': 'example_relay_http', 'terminal': True, 'probe': True, 'manual': 4,
                'field': {'kind': 'http', 'thing': 'http:url:desk_relay', 'channel': 'relay', 'allowed': ['ON', 'OFF'],
                          'settle': {'ON': 5000}, 'ack': 'result'}}})
            house = House(data); conf = Conf(); out = conf.yaml_file(module.FILE, ())
            module.render(data['devices'][0], house, out)
            descriptor = {'resources': {'desk_relay_relay': {}}}
            lifecycle_io.configure(house, conf, descriptor)
        actor = conf.files['automation/js/thermite_routing_io_desk_relay.js']
        config = json.loads(next(line for line in actor.splitlines() if line.startswith('const CONFIG = '))[len('const CONFIG = '):-1])
        self.assertEqual(config['fields'], {'desk_relay_relay': caps['desk_relay_relay']['field']})
        self.assertNotIn('192.0.2.20', actor)
        # Its 4 manual Items, two holds per resource and 17 per actor.
        self.assertEqual(descriptor['maxHolds'], 4 + 2 + 17)
        self.assertEqual(out.things['http:url:desk_relay']['channels']['relay']['config']['mode'], 'READONLY')

    def test_a_new_integration_with_a_rule_of_its_own_drives_its_fields(self):
        module = fictional('relay')
        data = self.inventory()
        with mock.patch.object(integrations, 'REGISTRY', (*integrations.REGISTRY, module)):
            house = House(data); conf = Conf(); out = conf.yaml_file(module.FILE, ())
            module.render(data['devices'][0], house, out)
            conf.files.update(module.files(data['devices'], house))
            lifecycle_io.configure(house, conf, {'resources': {'desk_relay_relay': {}}})
            text = conf.files['automation/js/example_relay_desk_relay.js']
            self.assertNotIn(lifecycle_io.CUSTOM_MARK, text)
            self.assertIn('"desk_relay_relay":{"kind":"relay","allowed":["ON","OFF"],"settle":{"ON":5000}}', text)
            self.assertNotIn('automation/js/thermite_routing_io_desk_relay.js', conf.files)
            del module.rule_path
            with self.assertRaisesRegex(ValueError, 'routing executor adapter is absent'):
                lifecycle_io.configure(house, Conf(), {'resources': {'desk_relay_relay': {}}})


class Denon(unittest.TestCase):
    """The receiver's routing actor is its integration's own rule."""

    def test_its_rule_renders_the_aliases_its_module_declares(self):
        text = denonmarantz_http.rule()
        line = next(line for line in text.splitlines() if line.startswith('const INPUT_ALIASES = '))
        self.assertEqual(json.loads(line[len('const INPUT_ALIASES = '):-1]), denonmarantz_http.INPUT_ALIASES)
        self.assertEqual(text.splitlines().count(lifecycle_io.CUSTOM_MARK), 1)
        self.assertEqual(text.count('const SEND = true;'), 1)
        self.assertNotIn('GAME', denonmarantz_http.SOURCE.read_text(encoding='utf-8'))

    def test_files_render_one_actor_per_receiver_under_control(self):
        from tests.test_zone_sync import HOUSE
        avr = next(device for device in HOUSE['devices'] if device['id'] == 'example_avr')
        watched = {**avr, 'id': 'example_watched', 'controls': {'observe': [denonmarantz_http.NAME]}}
        files = denonmarantz_http.files([avr, watched], House(HOUSE))
        self.assertIn(denonmarantz_http.rule_path(avr), files)
        self.assertNotIn(denonmarantz_http.rule_path(watched), files)

    def test_the_lifecycle_fills_its_rule_with_each_zone(self):
        import oh_render
        from tests.test_zone_sync import house
        files = oh_render.render(house())
        text = files[denonmarantz_http.rule_path({'id': 'example_avr'})]
        self.assertNotIn('automation/js/thermite_routing_io_example_avr.js', files)
        config = json.loads(next(line for line in text.splitlines() if line.startswith('const ROUTING_EXECUTOR = '))[len('const ROUTING_EXECUTOR = '):-1])
        self.assertEqual({item: (field['zone'], field['point']) for item, field in config['fields'].items()},
                         {'example_avr_main_power': (1, 'power'), 'example_avr_main_input': (1, 'input'),
                          'example_avr_zone2_power': (2, 'power'), 'example_avr_zone2_input': (2, 'input')})
        self.assertIn('SOURCE', config['fields']['example_avr_zone2_input']['allowed'])
        self.assertNotIn('SOURCE', config['fields']['example_avr_main_input']['allowed'])
        self.assertTrue(all(field == {'kind': 'denon', 'thing': 'denonmarantz:avr:example_avr', **{k: field[k] for k in ('zone', 'point', 'allowed')}}
                            for field in config['fields'].values()))

    @unittest.skipUnless(shutil.which('node'), 'node required')
    def test_its_provider_on_the_generic_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            rule = Path(directory) / 'denonmarantz_http.js'
            rule.write_text(denonmarantz_http.rule(), encoding='utf-8')
            result = subprocess.run([shutil.which('node'), str(AREA / 'tests/denonmarantz_http_harness.js'), str(lifecycle_io.SOURCE), str(rule)],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['passed'], 12)


@unittest.skipUnless(shutil.which('node'),'node required')
class Broker(unittest.TestCase):
    def test_actual_executor_source(self):
        result=subprocess.run([shutil.which('node'),str(AREA/'tests/lifecycle_io_harness.js'),str(lifecycle_io.SOURCE),str(pyatv_http.SOURCE)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['passed'],44)


class Quiescence(unittest.TestCase):
    def test_activity_snapshot_is_bounded_and_contains_only_nonnegative_counts(self):
        idle=dict(pending=0,active=0,quarantined=0,eventsQueued=0)
        self.assertEqual(routing_check.executor_activity(json.dumps(idle)),idle)
        self.assertEqual(routing_check.executor_activity(json.dumps({**idle,'quarantinedKeys':[]})),idle)
        held=dict(idle,quarantined=2)
        self.assertEqual(routing_check.executor_activity(json.dumps({**held,'quarantinedKeys':['example_avr','example_tv']})),held)
        bad=[None,'UNDEF','not JSON','{}','[]',' '*(routing_check.ACTIVITY_BYTES+1)]
        bad += [json.dumps({**idle,**change}) for change in ({'active':True},{'pending':-1},{'active':1.5},{'extra':'ticket'},
                                                            {'quarantinedKeys':'example_avr'},{'quarantinedKeys':['example_avr']},
                                                            {'quarantined':2,'quarantinedKeys':['example_tv','example_avr']},
                                                            {'quarantined':2,'quarantinedKeys':['example_avr','example_avr']},
                                                            {'quarantined':1,'quarantinedKeys':['Bad key']},{'quarantined':1,'quarantinedKeys':[1]})]
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
            denon=directory/'denonmarantz_http.js';denon.write_text(denonmarantz_http.rule(),encoding='utf-8')
            try:
                result=subprocess.run([java,'-cp',temporary+os.pathsep+classpath,'NativeLifecycleTest',str(lifecycle_io.SOURCE),
                                       str(AREA/'lib/oh_integrations/_io.js'),str(directory/'userdata'),str(sony_ip_control.SOURCE),str(denon)],
                                      capture_output=True,text=True,timeout=35)
            except subprocess.TimeoutExpired as error:
                self.fail('native broker timeout: '+str(error.stdout)+' '+str(error.stderr))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('native broker isolation, actor concurrency, durable uncertainty and reload fencing passed',result.stdout)
