from __future__ import print_function
from six import iteritems
from shutil import move
import os
import sys
import json
import getpass
import requests
import copy
import re
import zipfile
import tempfile

try:
    # Python 3
    from urllib.parse import urlparse
except ImportError:
    # Python 2
    from urlparse import urlparse

from openmdao.devtools.problem_viewer.problem_viewer import _get_viewer_data, view_model
from openmdao.api import IndepVarComp, Problem, Group, CaseReader
from tabulate import tabulate
from whatsopt import __version__

WHATSOPT_DIRNAME = os.path.join(os.path.expanduser('~'), '.whatsopt')
API_KEY_FILENAME = os.path.join(WHATSOPT_DIRNAME, 'api_key')
URL_FILENAME = os.path.join(WHATSOPT_DIRNAME, 'url')
NULL_DRIVER_NAME = '__DRIVER__'  # check WhatsOpt Discipline model

PROD_URL = "https://selene.onecert.fr/whatsopt"

DEBUG = False

class WhatsOptImportMdaError(Exception):
    pass

class WhatsOpt(object):

    def __init__(self, url=None, api_key=None, login=True):
        if url:
            self._url = url
        elif os.path.exists(URL_FILENAME):
            with open(URL_FILENAME, 'r') as f:
                self._url = f.read()
        else:
            self._url = self.default_url
        
        # config session object
        self.session = requests.Session()  
        self.set_trust_env() 
        
        # login by default
        if login:
            self.login(api_key)

        # save url
        if not os.path.exists(WHATSOPT_DIRNAME):
            os.makedirs(WHATSOPT_DIRNAME)
        with open(URL_FILENAME, 'w') as f:
            f.write(self._url)       

    @property
    def url(self):
        return self._url

    def _endpoint(self, path):
        return self._url + path

    def set_trust_env(self):
        urlinfos = urlparse(self._url)
        self.session.trust_env = re.match(r"\w+.onera\.fr", urlinfos.netloc)

    @property
    def default_url(self):
        self._default_url = PROD_URL
        return self._default_url    
            
    def _ask_and_write_api_key(self):
        print("You have to set your API key.")
        print("You can get it in your profile page on WhatsOpt (%s)." % self.url)
        print("Please, copy/paste your API key below then hit return (characters are hidden).")
        api_key = getpass.getpass(prompt='Your API key: ')
        if not os.path.exists(WHATSOPT_DIRNAME):
            os.makedirs(WHATSOPT_DIRNAME)
        with open(API_KEY_FILENAME, 'w') as f:
            f.write(api_key)
        return api_key 

    def _read_api_key(self):
        with open(API_KEY_FILENAME, 'r') as f:
            api_key = f.read()
            return api_key

    def login(self, api_key=None, echo=None):
        already_logged=False
        if api_key:
            self.api_key = api_key
        elif os.path.exists(API_KEY_FILENAME):
            already_logged=True
            self.api_key = self._read_api_key()
        else:
            self.api_key = self._ask_and_write_api_key()
        self.headers = {'Authorization': 'Token token=' + self.api_key, 'User-Agent': 'wop/{}'.format(__version__)}
        
        url =  self._endpoint('/api/v1/analyses')
        resp = self.session.get(url, headers=self.headers)
        if not api_key and already_logged and not resp.ok:
            self.logout(echo=False)  # log out silently, suppose one was logged on another server
            resp = self.login(api_key, echo)
        resp.raise_for_status() 
        if echo:
            print("Successfully logged into WhatsOpt (%s)" % self.url)
        return resp

    def logout(self, echo=True):
        if os.path.exists(API_KEY_FILENAME):
            os.remove(API_KEY_FILENAME)
        if os.path.exists(URL_FILENAME):
            os.remove(URL_FILENAME)
        if echo:
            print("Sucessfully logged out from WhatsOpt (%s)" % self.url)

    def list_analyses(self):
        url =  self._endpoint('/api/v1/analyses')
        resp = self.session.get(url, headers=self.headers)
        if resp.ok:
            mdas = resp.json()
            headers = ["id", "name", "created at"]
            data = []
            for mda in mdas:
                date = mda.get('created_at', None)
                data.append([mda['id'], mda['name'], date])
            print("Server : {}".format(self._url))
            print(tabulate(data, headers))
        else:
            resp.raise_for_status()

    def execute(self, progname, func, options):
        dir = os.path.dirname(progname)
        sys.path.insert(0, dir)
        with open(progname, 'rb') as fp:
            code = compile(fp.read(), progname, 'exec')
        globals_dict = {
            '__file__': progname,
            '__name__': '__main__',
            '__package__': None,
            '__cached__': None,
        }
        Problem._post_setup_func = func(options)
        sys.argv=[progname] # suppose progname do not need options
        exec(code, globals_dict)

    def push_mda_cmd(self, options):
        def push_mda(prob):
            name = options['--name']
            pbname = prob.model.__class__.__name__
            if name and pbname != name:
                print("Analysis %s skipped" % pbname)
                pass # do not exit
            else:
                self.push_mda(prob, options)
                exit()
                
        return push_mda

    def push_mda(self, problem, options):
        name = problem.model.__class__.__name__
        data = _get_viewer_data(problem)
        #print(name, data)
        self.scalar_format = options['--scalar-format']
        self.tree = data['tree']
        #print("TREE("+name+")=", self.tree)
        self.connections = data['connections_list']
        #print("CONNECTIONS("+name+")=", self.connections)

        # MDA informations
        self.vars = {}
        self.vardescs = {}
        self.discmap = {}
        self._collect_disc_infos(problem.model, self.tree)
        self._collect_var_infos(problem.model)
        mda_attrs = self._get_mda_attributes(problem.model, self.tree)

        if options['--dry-run']:
            print(json.dumps(mda_attrs, indent=2))
            # print(self.discmap)
        else:
            url =  self._endpoint('/api/v1/analyses')
            resp = self.session.post(url, headers=self.headers, json={'analysis': mda_attrs})
            resp.raise_for_status()
            print("Analysis %s pushed" % name)

    def pull_mda(self, mda_id, options={}, msg=None):
        if not msg: msg = 'Analysis %s pulled' % mda_id
        base = '_base' if options.get('--base') else '' 
        param = ''
        if options.get('--server'):
            param += '&with_server=true'
        if options.get('--run-ops'):
            param += '&with_runops=true'
        if options.get('--test-units'):
            param += '&with_unittests=true'
        if param is not '': param='?'+param[1:] 
        url =  self._endpoint(('/api/v1/analyses/%s/exports/new.openmdao'+base+param) % mda_id)
        resp = self.session.get(url, headers=self.headers, stream=True)
        resp.raise_for_status()
        name = None
        with tempfile.NamedTemporaryFile(suffix='.zip', mode='wb', delete=False) as fd:
            for chunk in resp.iter_content(chunk_size=128):
                fd.write(chunk)
            name = fd.name
        zip = zipfile.ZipFile(name, 'r')
        tempdir = tempfile.mkdtemp(suffix='wop', dir=tempfile.tempdir)
        zip.extractall(tempdir)
        filenames = zip.namelist()
        zip.close()
        for f in filenames:
            file_from = os.path.join(tempdir, f)
            file_to = f
            if os.path.exists(file_to):
                # if re.match(r"run_analysis.py", f):
                #     continue
                # elif options.get('--force'):
                if options.get('--force'):
                    print("Update %s" % file_to)
                    if not options.get('--dry-run'):
                        os.remove(file_to)
                else:
                    print("File %s in the way" % file_to)
                    exit(-1)
            else:
                print("Pull %s" % file_to) 
        if not options.get('--dry-run'):
            for f in filenames:
                file_from = os.path.join(tempdir, f)
                dir_to = os.path.dirname(f)
                if dir_to == "":
                    dir_to = '.'
                elif not os.path.exists(dir_to):
                    os.makedirs(dir_to)
                # print("Move {} to {}".format(file_from, dir_to))
                # if os.path.exists(dir_to) and re.match(r"run_analysis.py", f):
                #     continue
                move(file_from, dir_to)
            print(msg)
    
    def update_mda(self, analysis_id=None, options={}):
        id = analysis_id or self.get_analysis_id()
        opts = copy.copy(options)
        opts.update({'--base': True, '--force': True})
        self.pull_mda(id, opts, 'Analysis %s updated' % id)
        
    def upload(self, sqlite_filename, analysis_id=None, operation_id=None, cleanup=False, dry_run=False):
        from socket import gethostname
        mda_id = self.get_analysis_id() if not analysis_id else analysis_id
        reader = CaseReader(sqlite_filename)
        cases = reader.list_cases('driver')
        if len(cases)==0:
            raise Exception("No case found in {}".format(sqlite_filename))

        driver_first_coord = cases[0]
        name = os.path.splitext(sqlite_filename)[0]
        m = re.match(r"\w+:(\w+)|.*", driver_first_coord)
        if m:
            name = m.group(1)
        cases, statuses = self._format_upload_cases(reader)
        if dry_run:
            WhatsOpt._print_cases(cases, statuses)
            exit()
        resp = None
        if operation_id:
            url =  self._endpoint(('/api/v1/operations/%s') % operation_id)
            operation_params = {'cases': cases}
            resp = self.session.patch(url, headers=self.headers, 
                                      json={'operation': operation_params})
        else:
            url =  self._endpoint(('/api/v1/analyses/%s/operations') % mda_id)
            if name=='LHS':
                driver='smt_doe_lhs'
            elif name=='Morris':
                driver='salib_doe_morris'
            elif name=='SLSQP':
                driver='scipy_optimizer_slsqp'
            else:
                # suppose name well-formed <lib>-<doe|optimizer|screening>-<algoname>
                # otherwise it will default to doe
                driver=name.lower()  
            operation_params = {'name': name,
                                'driver': driver,
                                'host': gethostname(),
                                'cases': cases,
                                'success': statuses}
            resp = self.session.post(url, headers=self.headers, 
                                     json={'operation': operation_params})
        resp.raise_for_status()
        print("Results data from {} uploaded with driver {}".format(sqlite_filename, driver))
        if cleanup:
            os.remove(sqlite_filename)
            print("%s removed" % sqlite_filename)

    def check_versions(self):
        url =  self._endpoint('/api/v1/versioning')
        resp = self.session.get(url, headers=self.headers)
        resp.raise_for_status()
        version = resp.json()
        print("WhatsOpt:{} recommended wop:{}".format(version['whatsopt'], version['wop']))
        print("current wop:{}".format(__version__))
        
    def serve(self):
        from subprocess import call
        try:
            import thrift
        except ImportError:
            print("Apache Thrift is not installed. You can install it with : 'pip install thrift'")
            exit(-1)
        call(['python', 'run_server.py'])
        
    def get_analysis_id(self):
        files = self._find_analysis_base_files()
        id = None
        for f in files:
            ident = self._extract_mda_id(f) 
            if id and id != ident:
                raise Exception('Warning: several analysis identifier detected. \n'
                                'Find %s got %s. Check header comments in %s files .' % (id, ident, str(files)))  
            id = ident    
        return id 
        
    @staticmethod
    def _find_analysis_base_files():
        files = []
        for f in os.listdir("."):
            if f.endswith("_base.py"):
                files.append(f)
        return files    
    
    @staticmethod
    def _extract_mda_id(file):
        ident = None
        with open(file, 'r') as f:
            for line in f:
                match = re.match(r"^# analysis_id: (\d+)", line) 
                if match:
                    ident = match.group(1)
                    break
        return ident
    
    @staticmethod
    def _extract_mda_name(name):
        match = re.match(r"(\w+)_\w+.sqlite", name)
        if match:
            return match.group(1)
        else:
            return 'mda'

    # # see _get_tree_dict at
    # # https://github.com/OpenMDAO/OpenMDAO/blob/master/openmdao/devtools/problem_viewer/problem_viewer.py
    def _collect_disc_infos(self, system, tree, group_prefix=''):
        if 'children' not in tree:
            return

        for i, child in enumerate(tree['children']):
            # retain only components, not intermediates (subsystem or group)
            if child['type'] == 'subsystem' and child['subsystem_type'] == 'group':
                self.discmap[group_prefix+child['name']] = child['name']
                prefix = group_prefix+child['name']+'.'
                self._collect_disc_infos(system._subsystems_myproc[i], child, prefix)
            else:
                # do not represent IndepVarComp
                if not isinstance(system._subsystems_myproc[i], IndepVarComp):
                    self.discmap[group_prefix+child['name']] = child['name']
                else:
                    self.discmap[group_prefix+child['name']] = '__DRIVER__'


    # see _get_tree_dict at
    # https://github.com/OpenMDAO/OpenMDAO/blob/master/openmdao/devtools/problem_viewer/problem_viewer.py
    def _collect_var_infos(self, system):
        for typ in ['input', 'output']:
            for abs_name in system._var_abs_names[typ]:
                io_mode = 'out'
                if typ == 'input': 
                    io_mode = 'in' 
                elif typ == 'output': 
                    io_mode = 'out'
                else:   
                    raise Exception('Unhandled variable type ' + typ)
                meta = system._var_abs2meta[abs_name]

                vtype = 'Float'
                if re.match('int', type(meta['value']).__name__):
                    vtype = 'Integer' 
                shape = str(meta['shape']) 
                shape = self._format_shape(shape)
                name = system._var_abs2prom[typ][abs_name]
                self.vars[abs_name] = {'fullname': abs_name,
                                        'name': name,
                                        'io_mode': io_mode,
                                        'type': vtype,
                                        'shape': shape,
                                        'units': meta['units'],
                                        #'desc': meta['desc'],
                                        'value': meta['value']}

                # retrieve initial conditions
                var = self.vars[abs_name]
                if abs_name in system._outputs._views:
                    var['value'] = system._outputs[abs_name]
                elif abs_name in system._inputs._views:
                    var['value'] = system._inputs[abs_name]
                elif abs_name in system._discrete_outputs:
                    var['value'] = system._discrete_outputs[abs_name]
                elif abs_name in system._discrete_inputs:
                    var['value'] = system._discrete_inputs[abs_name]

                desc = self.vardescs.setdefault(name, '')
                if desc=='':
                    self.vardescs[name] = meta['desc'] 
                elif desc!=meta['desc'] and meta['desc']!='':
                    print('Find another description for {}: "{}", keep "{}"'.format(name, meta['desc'], self.vardescs[name]))

    def _format_shape(self, shape):
        shape = shape.replace("L", "")  # with py27 we can get (1L,)
        if self.scalar_format and shape=='(1,)':
            shape='1'
        return shape

    def _get_mda_attributes(self, group, tree, group_prefix=''):
        driver_attrs = {'name': NULL_DRIVER_NAME, 'variables_attributes': []}
        mda_attrs = {'name': group.__class__.__name__, 'disciplines_attributes': [driver_attrs]}
        if 'children' not in tree:
            return

        for i, child in enumerate(tree['children']):
            if child['type'] == 'subsystem' and child['subsystem_type'] == 'group':
                prefix = group_prefix+child['name']+'.'
                sub_analysis_attrs = self._get_sub_analysis_attributes(group._subsystems_myproc[i], child, prefix)
                mda_attrs['disciplines_attributes'].append(sub_analysis_attrs)
            else:
                if not isinstance(group._subsystems_myproc[i], IndepVarComp):
                    mda = group_prefix[:-1]
                    discname = group_prefix+child['name']
                    discattrs = self._get_discipline_attributes(driver_attrs, mda, discname)

                    self._set_varattrs_from_outputs(group._subsystems_myproc[i]._var_abs2prom['output'], 'out',
                                                    discattrs['variables_attributes'])

                    mda_attrs['disciplines_attributes'].append(discattrs)
                else:
                    self._set_varattrs_from_outputs(group._subsystems_myproc[i]._var_abs2prom['output'], 
                                                    'out', driver_attrs['variables_attributes'])

        self._set_varattrs_from_outputs(group._var_abs2prom['output'], 'in', 
                                        driver_attrs['variables_attributes'])

        # remove fullname in driver varattrs
        for vattr in driver_attrs['variables_attributes']:
            vattr['desc'] = self.vardescs[vattr['name']]
            if vattr['io_mode']=='out':  # set init value for design variables and parameters (outputs of driver)
                v = self.vars[vattr['fullname']]
                vattr['parameter_attributes'] = {'init': self._simple_value(v)}
            if 'fullname' in vattr:
                del vattr['fullname'] # indeed for WhatsOpt var name is a primary key

        for discattr in mda_attrs['disciplines_attributes']:
            if 'variables_attributes' in discattr:
                for vattr in discattr['variables_attributes']:
                    if 'fullname' in vattr:
                        del vattr['fullname'] # indeed for WhatsOpt var name is a primary key


        return mda_attrs

    @staticmethod
    def _to_camelcase(name):
        return re.sub(r'(?:^|_)(\w)', lambda x: x.group(1).upper(), name)

    def _get_sub_analysis_attributes(self, group, child, prefix):
        submda_attrs = self._get_mda_attributes(group, child, prefix)
        submda_attrs['name'] = WhatsOpt._to_camelcase(child['name'])
        superdisc_attrs = {'name': child['name'], 'sub_analysis_attributes': submda_attrs}
        return superdisc_attrs

    def _get_discipline_attributes(self, driver_attrs, mda, dname):
        varattrs = self._get_variables_attrs(driver_attrs['variables_attributes'], mda, dname)
        discattrs = {'name': WhatsOpt._to_camelcase(self.discmap[dname]), 'variables_attributes': varattrs}
        return discattrs

    def _get_variables_attrs(self, driver_varattrs, mda, dname):
        varattrs = []
        for conn in self.connections:
            self._get_varattr_from_connection(varattrs, driver_varattrs, mda, dname, conn)
        for vattr in varattrs:
            vattr['desc'] = self.vardescs[vattr['name']]
            if 'fullname' in vattr:
                del vattr['fullname'] # indeed for WhatsOpt var name is a primary key
        return varattrs
            
    def _get_varattr_from_connection(self, varattrs, driver_varattrs, mda, dname, connection):
        fnamesrc = connection['src']
        mdasrc, discsrc, varsrc = WhatsOpt._extract_disc_var(fnamesrc)
        fnametgt = connection['tgt']
        mdatgt, disctgt, vartgt = WhatsOpt._extract_disc_var(fnametgt)
        if DEBUG:
            print('++++ MDA=%s DISC=%s' % (mda, dname))
            print('######### SRC=%s DISCSRC=%s TGT=%s DISCTGT=%s' % (mdasrc, discsrc, mdatgt, disctgt))
            
        varstoadd = []
        if mda == mdasrc and discsrc == dname:
            varattrsrc = {'name':varsrc, 'fullname': fnamesrc, 'io_mode': 'out',
                            'type':self.vars[fnamesrc]['type'], 'shape':self.vars[fnamesrc]['shape'], 
                            'units':self.vars[fnamesrc]['units']}
            varstoadd.append((discsrc, varattrsrc, "source"))
            if (mda != '' and mda not in mdatgt):
                discsrc = NULL_DRIVER_NAME
                varattrsrc = {'name':varsrc, 'fullname': fnamesrc, 'io_mode': 'in',
                              'type':self.vars[fnametgt]['type'], 'shape':self.vars[fnametgt]['shape'], 
                              'units':self.vars[fnametgt]['units']}
                varstoadd.append((discsrc, varattrsrc, "source"))

        if mda == mdatgt and disctgt == dname:
            varattrtgt = {'name':vartgt, 'fullname': fnametgt, 'io_mode': 'in',
                          'type':self.vars[fnametgt]['type'], 'shape':self.vars[fnametgt]['shape'], 
                          'units':self.vars[fnametgt]['units']}
            varstoadd.append((disctgt, varattrtgt, "target"))
            if (mda != '' and mda not in mdasrc):
                disctgt = NULL_DRIVER_NAME
                varattrtgt = {'name':vartgt, 'fullname': fnametgt, 'io_mode': 'out',
                              'type':self.vars[fnamesrc]['type'], 'shape':self.vars[fnamesrc]['shape'], 
                              'units':self.vars[fnamesrc]['units']}
                varstoadd.append((disctgt, varattrtgt, "target"))

        for disc, varattr, orig in varstoadd:
            if DEBUG:
                print("**************", connection)
            if disc==dname:
                if (varattr not in varattrs):
                    if DEBUG:
                        print(">>>>>>>>>>>>> from", orig ," ADD to ", mda, dname, ": ", varattr['name'], varattr['io_mode']) 
                    varattrs.append(varattr)
            else:
                if varattr['name'] not in [vattr['name'] for vattr in driver_varattrs]:
                    if DEBUG:
                        print(">>>>>>>>>>>>> from", orig ," ADD to ", mda, "__DRIVER__ :", varattr['name'], varattr['io_mode']) 
                    driver_varattrs.append(varattr)

    def _set_varattrs_from_outputs(self, outputs, io_mode, varattrs):
        for absname, varname in iteritems(outputs):
            if varname not in [varattr['name'] for varattr in varattrs]:
                var = self.vars[absname] 
                vattr = {'name': varname, 'fullname': absname, 'io_mode': io_mode, 'desc': self.vardescs[varname],
                        'type':var['type'], 'shape':var['shape'], 'units':var['units']}
                varattrs.append(vattr)

    @staticmethod
    def _simple_value(var):
        if var['shape']=='1' or var['shape']=='(1,)':
            ret = float(var['value'])
            if type=='Integer':
                ret = int(ret)
        else:
            if type=='Integer':
                var['value'] = var['value'].astype(int)
            else:
                var['value'] = var['value'].astype(float)
            ret = var['value'].tolist()
        return str(ret)
        
    @staticmethod
    def _extract_disc_var(fullname):
        name_elts = fullname.split('.')
        if len(name_elts) > 1:
            mda, disc, var = '.'.join(name_elts[:-2]), '.'.join(name_elts[:-1]), name_elts[-1] 
        else:
            raise Exception('Connection qualified name should contain' + 
                            ' at least one dot, but got %s' % fullname)
        return mda, disc, var

    def _format_upload_cases(self, reader):
        cases = reader.list_cases('root', recurse=False)
        inputs = {}
        outputs = {}
        for case_id in cases:
            if "compute_totals_approx" not in case_id:
                case = reader.get_case(case_id)
                if case.inputs is not None:
                    self._insert_data(case.inputs, inputs)
                if case.outputs is not None:
                    self._insert_data(case.outputs, outputs)
        cases = inputs.copy()
        cases.update(outputs)
        inputs_count = self._check_count(inputs)
        outputs_count = self._check_count(outputs)
        assert inputs_count==outputs_count
        data = []
        for key, values in iteritems(cases):
            idx = key[1]
            if key[2] == 1:
                idx = -1 # consider it is a scalar not an array of 1 elt
            data.append({'varname': key[0], 'coord_index': idx, 'values': values})
        
        statuses = []
        cases = reader.list_cases('driver', recurse=False)
        for case_id in cases:
            #if driver_regexp.match(case_id):
            case = reader.get_case(case_id)
            statuses.append(case.success)
        assert inputs_count==len(statuses)

        return data, statuses
        
    def _check_count(self, ios):
        count = None
        for name in ios:
            if count and count != len(ios[name]):
                raise Exception('Bad value count between (%s, %d) and (%s, %d)' % \
                                (refname, count, name, len(ios[name])))
            else:
                refname, count = name, len(ios[name])
        return count
                            
    def _insert_data(self, data_io, result):
        done = {}
        for n in data_io._values.dtype.names:
            values = data_io._values[n]
            name = n.split('.')[-1]
            if name in done:
                continue
            values = values.reshape(-1)
            for i in range(values.size):
                if (name, i, values.size) in result:
                    result[(name, i, values.size)].append(float(values[i]))
                else:
                    result[(name, i, values.size)] = [float(values[i])]
            done[name]=True

    @staticmethod
    def _print_cases(cases, statuses):
        headers = ["success"]
        n = len(cases[0]['values']) if cases else 0
        for case in cases:
            h = case['varname']
            if case['coord_index'] > -1:
                h += "[{}]".format(case['coord_index'])
            headers.append(h)
        data = []
        for i in range(n):
            data.append([statuses[i]]+[case['values'][i] for case in cases])
        print(tabulate(data, headers))