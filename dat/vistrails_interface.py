"""Interface with VisTrails.

This module contains most of the code that deals with VisTrails pipelines.
"""

import copy
import importlib
import inspect
from itertools import chain, izip
import os
import sys
import warnings

from PyQt4 import QtCore, QtGui

from dat import BaseVariableLoader, PipelineInformation, RecipeParameterValue
from dat.gui import translate

from vistrails.core import get_vistrails_application
from vistrails.core.db.action import create_action
from vistrails.core.db.locator import XMLFileLocator
from vistrails.core.layout.workflow_layout import Pipeline as LayoutPipeline, \
    WorkflowLayout
from vistrails.core.modules.basic_modules import Constant
from vistrails.core.modules.module_descriptor import ModuleDescriptor
from vistrails.core.modules.module_registry import get_module_registry
from vistrails.core.modules.sub_module import InputPort
from vistrails.core.modules.utils import parse_descriptor_string
from vistrails.core.modules.vistrails_module import Module
from vistrails.core.utils import DummyView
from vistrails.core.vistrail.controller import VistrailController
from vistrails.core.vistrail.connection import Connection
from vistrails.core.vistrail.location import Location
from vistrails.core.vistrail.module import Module as PipelineModule
from vistrails.core.vistrail.pipeline import Pipeline
from vistrails.gui.theme import CurrentTheme
from vistrails.gui.modules import get_widget_class
from vistrails.packages.spreadsheet.basic_widgets import CellLocation, \
    SpreadsheetCell, SheetReference


class CancelExecution(RuntimeError):
    pass


def resolve_descriptor(param, package_identifier=None):
    """Resolve a type specifier to a ModuleDescriptor.

    This accepts different arguments and turns it into a ModuleDescriptor:
      * a ModuleDescriptor
      * a Module object
      * a descriptor string

    It should be used when accepting type specifiers from third-party code.

    The optional 'package_identifier' parameter gives the context in which to
    resolve module names; it is passed to parse_descriptor_string().
    """
    reg = get_module_registry()

    if isinstance(param, str):
        d_tuple = parse_descriptor_string(param, package_identifier)
        return reg.get_descriptor_by_name(*d_tuple)
    elif isinstance(param, type) and issubclass(param, Module):
        return reg.get_descriptor(param)
    elif isinstance(param, ModuleDescriptor):
        return param
    else:
        raise TypeError("resolve_descriptor() argument must be a Module "
                        "subclass or str object, not '%s'" % type(param))


class ModuleWrapper(object):
    """Object representing a VisTrails module in a DAT variable pipeline.

    This is a wrapper returned by Variable#add_module. It is used by VisTrails
    packages to build a pipeline for a new variable.
    """
    def __init__(self, variable, module_type):
        self._variable = variable
        descriptor = resolve_descriptor(module_type,
                                        self._variable._vt_package_id)
        controller = self._variable._generator.controller
        self._module = controller.create_module_from_descriptor(descriptor)
        self._variable._generator.add_module(self._module)

    def add_function(self, inputport_name, vt_type, value):
        """Add a function for a port of this module.

        vt_type is resolvable to a VisTrails module type (or a list of types).
        value is the value as a string (or a list of strings; the length should
        be the same as vt_type's).
        """
        # Check port name
        port = None
        for p in self._module.destinationPorts():
            if p.name == inputport_name:
                port = p
                break

        if port is None:
            raise ValueError("add_function() called for a non-existent input "
                             "port")

        # Check types
        if not isinstance(vt_type, (tuple, list)):
            vt_type = (vt_type,)
        if not isinstance(value, (tuple, list)):
            value = [value]
        if len(vt_type) != len(value):
            raise ValueError("add_function() received different numbers of "
                             "types and values")
        if len(vt_type) != len(port.descriptors()):
            raise ValueError("add_function() called with a different number "
                             "of values from the given input port")
        for t_param, p_descr in izip(vt_type, port.descriptors()):
            t_descr = resolve_descriptor(t_param,
                                         self._variable._vt_package_id)
            if not issubclass(t_descr.module, p_descr.module):
                raise ValueError("add_function() called with incompatible "
                                 "types")

        self._variable._generator.update_function(
                self._module,
                inputport_name,
                value)

    def connect_outputport_to(self, outputport_name, other_module, inputport_name):
        """Create a connection between ports of two modules.

        Connects the given output port of this module to the given input port
        of another module.

        The modules must be wrappers for the same Variable.
        """
        if self._variable is not other_module._variable:
            raise ValueError("connect_outputport_to() can only connect "
                             "modules of the same Variable")
        # Might raise vistrails.core.modules.module_registry:MissingPort
        self._variable._generator.connect_modules(
                self._module, outputport_name,
                other_module._module, inputport_name)


class Variable(object):
    """Object used to build a DAT variable.

    This is a wrapper used by VisTrails packages to build a pipeline for a new
    variable. This variable is then stored in the VistrailData.
    Wrapper objects are restored from the Vistrail file easily: they are
    children versions of the version tagged 'dat-vars', and have a tag
    'dat-var-name' where 'name' is the name of that specific DAT variable.
    """

    class VariableInformation(object):
        """Object actually representing a DAT variable.

        Because most of the logic/attribute in Variable become unnecessary once
        the Variable has been materialized in the pipeline, this is the actual
        class of the object we store. It is created by
        Variable#perform_operations().
        """
        def __init__(self, name, controller, type):
            self.name = name
            self._controller = controller
            self.type = type

        def remove(self):
            """Delete the pipeline from the Vistrail.

            This is called by the VistrailData when the Variable is removed.
            """
            controller = self._controller
            version = controller.vistrail.get_version_number(
                    'dat-var-%s' % self.name)
            controller.prune_versions([version])

        def rename(self, new_varname):
            """Change the tag on this version in the Vistrail.

            This is called by the VistrailData when the Variable is renamed.
            """
            controller = self._controller
            version = controller.vistrail.get_version_number(
                    'dat-var-%s' % self.name)
            controller.vistrail.set_tag(version, 'dat-var-%s' % new_varname)

            self.name = new_varname

    @staticmethod
    def _get_variables_root(controller=None):
        """Create or get the version tagged 'dat-vars'

        This is the base version of all DAT variables. It consists of a single
        OutputPort module with name 'value'.
        """
        if controller is None:
            controller = get_vistrails_application().get_controller()
        if controller.vistrail.has_tag_str('dat-vars'):
            root_version = controller.vistrail.get_version_number('dat-vars')
        else:
            # Create the 'dat-vars' version
            controller.change_selected_version(0)
            controller.add_module_action
            reg = get_module_registry()
            operations = []

            # Add an OutputPort module
            descriptor = reg.get_descriptor_by_name(
                    'edu.utah.sci.vistrails.basic', 'OutputPort')
            out_mod = controller.create_module_from_descriptor(descriptor)
            operations.append(('add', out_mod))

            # Add a function to this module
            operations.extend(
                    controller.update_function_ops(
                            out_mod,
                            'name',
                            ['value']))

            # Perform the operations
            action = create_action(operations)
            controller.add_new_action(action)
            root_version = controller.perform_action(action)
            # Tag as 'dat-vars'
            controller.vistrail.set_tag(root_version, 'dat-vars')

        controller.change_selected_version(root_version)
        pipeline = controller.current_pipeline
        outmod_id = pipeline.modules.keys()
        assert len(outmod_id) == 1
        outmod_id = outmod_id[0]
        return controller, root_version, outmod_id

    def __init__(self, type, controller=None, generator=None, output=None):
        """Create a new variable.

        type should be resolvable to a VisTrails module type.
        """
        # Create or get the version tagged 'dat-vars'
        controller, self._root_version, self._output_module_id = (
                Variable._get_variables_root(controller))

        self._output_module = None

        if generator is None:
            self._generator = PipelineGenerator(controller)
    
            # Get the VisTrails package that's creating this Variable by inspecting
            # the stack
            caller = inspect.currentframe().f_back
            try:
                module = inspect.getmodule(caller).__name__
                if module.endswith('.__init__'):
                    module = module[:-9]
                if module.endswith('.init'):
                    module = module[:-5]
                pkg = importlib.import_module(module)
                self._vt_package_id = pkg.identifier
            except (ImportError, AttributeError):
                self._vt_package_id = None
        else:
            self._generator = generator
            self._vt_package_id = None
            if output is not None:
                self._output_module, self._outputport_name = output

        self.type = resolve_descriptor(type, self._vt_package_id)

    def add_module(self, module_type):
        """Add a new module to the pipeline and return a wrapper.
        """
        return ModuleWrapper(self, module_type)

    def select_output_port(self, module, outputport_name):
        """Select the output port of the Variable pipeline.

        The given output port of the given module will be chosen as the output
        port of the Variable. It is this output port that will be connected to
        the Plot subworkflow's input port when creating an actual pipeline.

        The selected port should have a type that subclasses the Variable's
        declared type.

        This function should be called exactly once when creating a Variable.
        """
        # Connects the output port with the given name of the given wrapped
        # module to the OutputPort module (added at version 'dat-vars')
        if module._variable is not self:
            raise ValueError("select_output_port() designated a module from a "
                             "different Variable")
        elif self._output_module is not None:
            raise ValueError("select_output_port() was called more than once")

        # Check that the port is compatible to self.type
        try:
            port = module._module.get_port_spec(
                    outputport_name, 'output')
        except Exception:
            raise ValueError("select_output_port() designated a non-existent "
                             "port")
        # The designated output port has to be a subclass of self.type
        if len(port.descriptors()) != 1:
            raise ValueError("select_output_port() designated a port with "
                             "multiple types")
        if not issubclass(port.descriptors()[0].module, self.type.module):
            raise ValueError("select_output_port() designated a port with an "
                             "incompatible type")

        self._output_module = module._module
        self._outputport_name = outputport_name

    def perform_operations(self, name):
        """Materialize this Variable in the Vistrail.

        Create a pipeline tagged as 'dat-var-<varname>' for this Variable,
        children of the 'dat-vars' version.

        This is called by the VistrailData when the Variable is inserted.
        """
        if self._output_module is None:
            raise ValueError("Invalid Variable: select_output_port() was "
                             "never called")

        controller = self._generator.controller
        controller.change_selected_version(self._root_version)

        out_mod = controller.current_pipeline.modules[self._output_module_id]
        self._generator.connect_modules(
                self._output_module, self._outputport_name,
                out_mod, 'InternalPipe')

        self._generator.update_function(out_mod, 'spec', [self.type.sigstring])

        self._var_version = self._generator.perform_action()
        controller.vistrail.set_tag(self._var_version,
                                    'dat-var-%s' % name)
        controller.change_selected_version(self._var_version)

        return Variable.VariableInformation(name, controller, self.type)

    @staticmethod
    def read_type(pipeline):
        """Read the type of a Variable from its pipeline.

        The type is obtained from the 'spec' input port of the 'OutputPort'
        module.
        """
        reg = get_module_registry()
        OutputPort = reg.get_module_by_name(
                'edu.utah.sci.vistrails.basic', 'OutputPort')
        outputs = find_modules_by_type(pipeline, [OutputPort])
        if len(outputs) == 1:
            output = outputs[0]
            if get_function(output, 'name') == 'value':
                spec = get_function(output, 'spec')
                return resolve_descriptor(spec)
        return None

    @staticmethod
    def from_pipeline(controller, varname, type=None):
        pipeline = controller.vistrail.getPipeline('dat-var-%s' % varname)
        if type is None:
            type = Variable.read_type(pipeline)
        generator = PipelineGenerator(controller)
        output = add_variable_subworkflow(generator, pipeline)
        return Variable(
                type=type,
                controller=controller,
                generator=generator,
                output=output)


class ArgumentWrapper(object):
    def __init__(self, variable):
        self._variable = variable
        self._copied = False

    def connect_to(self, module, inputport_name):
        if not self._copied:
            # First, we need to copy this pipeline into the new Variable
            generator = module._variable._generator
            generator.append_operations(self._variable._generator.operations)
            self._copied = True
        generator.connect_modules(
                self._variable._output_module,
                self._variable._outputport_name,
                module._module,
                inputport_name)


def call_operation_callback(op, callback, args):
    """Call a VariableOperation callback to build a new Variable.

    op is the requested operation.
    callback is the VisTrails package's function that is wrapped here.
    args is a list of Variable that are the arguments of the operation; they
    need to be wrapped as the package is not supposed to manipulate these
    directly.
    """
    kwargs = dict()
    for i in xrange(len(args)):
        kwargs[op.parameters[i].name] = ArgumentWrapper(args[i])
    result = callback(**kwargs)
    for argname, arg in kwargs.iteritems():
        if not arg._copied:
            warnings.warn("In operation %r, argument %r was not used" %(
                          op.name, argname))
    return result


def apply_operation_subworkflow(controller, op, subworkflow, args):
    """Load an operation subworkflow from a file to build a new Variable.

    op is the requested operation.
    subworkflow is the filename of an XML file.
    args is a list of Variable that are the arguments of the operation; they
    will be connected in place of the operation subworkflow's InputPort
    modules.
    """
    reg = get_module_registry()
    inputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'InputPort')
    outputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'OutputPort')

    generator = PipelineGenerator(controller)

    # Add the operation subworkflow
    locator = XMLFileLocator(subworkflow)
    vistrail = locator.load()
    version = vistrail.get_latest_version()
    operation_pipeline = vistrail.getPipeline(version)

    # Copy every module but the InputPorts and the OutputPort
    operation_modules_map = dict() # old module id -> new module
    for module in operation_pipeline.modules.itervalues():
        if module.module_descriptor not in (inputport_desc, outputport_desc):
            operation_modules_map[module.id] = generator.copy_module(module)

    # Copy the connections and locate the input ports and the output port
    operation_params = dict() # param name -> [(module, input port name)]
    output = None # (module, port name)
    for connection in operation_pipeline.connection_list:
        src = operation_pipeline.modules[connection.source.moduleId]
        dest = operation_pipeline.modules[connection.destination.moduleId]
        if src.module_descriptor is inputport_desc:
            param = get_function(src, 'name')
            ports = operation_params.setdefault(param, [])
            ports.append((
                    operation_modules_map[connection.destination.moduleId],
                    connection.destination.name))
        elif dest.module_descriptor is outputport_desc:
            output = (operation_modules_map[connection.source.moduleId],
                      connection.source.name)
        else:
            generator.connect_modules(
                    operation_modules_map[connection.source.moduleId],
                    connection.source.name,
                    operation_modules_map[connection.destination.moduleId],
                    connection.destination.name)

    # Add the parameter subworkflows
    for i in xrange(len(args)):
        generator.append_operations(args[i]._generator.operations)
        o_mod = args[i]._output_module
        o_port = args[i]._outputport_name
        for i_mod, i_port in operation_params.get(op.parameters[i].name, []):
            generator.connect_modules(
                    o_mod, o_port,
                    i_mod, i_port)

    return Variable(
        type=op.return_type,
        controller=controller,
        generator=generator,
        output=output)


class CustomVariableLoader(QtGui.QWidget, BaseVariableLoader):
    """Custom variable loading tab.

    These loaders show up in a tab of their own, allowing to load any kind of
    data from any source.

    It is a widget that the user will use to choose the data he wants to load.
    load() will be called when the user confirms to actually create a Variable
    object.
    reset() is called to reset the widget to its original settings, so that it
    can be reused to load something else.
    get_default_variable_name() should return a sensible variable name for the
    variable that will be loaded; the user can edit it if need be.
    If the default variable name changes because of the user changing its
    selection, default_variable_name_changed() can be called to update it.
    """
    def __init__(self):
        QtGui.QWidget.__init__(self)
        BaseVariableLoader.__init__(self)

    def load(self):
        """Load the variable and return it.

        Implement this in subclasses to load whatever data the user selected as
        a Variable object.
        """
        raise NotImplementedError


class FileVariableLoader(QtGui.QWidget, BaseVariableLoader):
    """A loader that gets a variable from a file.

    Subclasses do not get a tab of their own, but appear on the "File" tab if
    they indicate they are able to load the selected file.
    """
    @classmethod
    def can_load(cls, filename):
        """Indicates whether this loader can read the given file.

        If true, it will be selectable by the user.
        You have to implement this in subclasses.

        Do not actually load the data here, you should only do quick checks
        (like file extension or magic number).
        """
        return False

    def __init__(self):
        """Constructor.

        This constructor receives a 'filename' parameter: the file that we want
        to load. Do not keep the file open thoughout the life of this object,
        it could interfere with other loaders.
        """
        QtGui.QWidget.__init__(self)
        BaseVariableLoader.__init__(self)

    def load(self):
        """Load the variable and return it.

        Implement this in subclasses to do the actual loading of the variable
        from the filename that was given to the constructor, using the desired
        parameters.
        """
        raise NotImplementedError


class Port(object):
    """A simple bean containing informations about one of a plot's port.

    These are optionally passed to Plot's constructor by a VisTrails package,
    else they will be built from the InputPort modules found in the pipeline.

    'accepts' can be either DATA, which means the port should receive a
    variable through drag and drop, or INPUT, which means the port will be
    settable through VisTrails's constant widgets. In the later case, the
    module type should be a constant.
    """
    DATA = 1
    INPUT = 2

    def __init__(self, name, type=None, optional=False, accepts=DATA):
        self.name = name
        self.type = type
        self.optional = optional
        self.accepts = accepts


class DataPort(Port):
    def __init__(self, *args, **kwargs):
        Port.__init__(self, *args, accepts=Port.DATA, **kwargs)


class ConstantPort(Port):
    def __init__(self, *args, **kwargs):
        Port.__init__(self, *args, accepts=Port.INPUT, **kwargs)


class Plot(object):
    def __init__(self, name, **kwargs):
        """A plot descriptor.

        Describes a Plot. These objects should be created by a VisTrails
        package for each Plot it wants to registers with DAT, and added to a
        global '_plots' variable in the 'init' module (for a reloadable
        package).

        name is mandatory and will be displayed to the user.
        description is a text that explains what your Plot is about, and can be
        localized.
        ports should be a list of Port objects describing the input your Plot
        expects.
        subworkflow is the path to the subworkflow that will be used for this
        Plot. In this string, '{package_dir}' will be replaced with the current
        package's path.
        """
        self.name = name
        self.description = kwargs.get('description')

        caller = inspect.currentframe().f_back
        package = os.path.dirname(inspect.getabsfile(caller))

        # Build plot from a subworkflow
        self.subworkflow = kwargs['subworkflow'].format(package_dir=package)
        self.ports = kwargs.get('ports', [])

        # Set the plot config widget, ensuring correct parent class
        from dat.gui.overlays import PlotConfigOverlay, \
            DefaultPlotConfigOverlay
        self.configWidget = kwargs.get('configWidget', DefaultPlotConfigOverlay)
        if not issubclass(self.configWidget, PlotConfigOverlay): 
            warnings.warn("Config widget of plot '%s' does not subclass "
                          "'PlotConfigOverlay'. Using default." % self.name)
            self.configWidget = DefaultPlotConfigOverlay

    def _read_metadata(self, package_identifier):
        """Reads a plot's ports from the subworkflow file
    
        Finds each InputPort module and gets the parameter name, optional flag
        and type from its 'name', 'optional' and 'spec' input functions.

        If the module type is a subclass of Constant, we will assume the port
        is to be set via direct input (ConstantPort), else by dragging a
        variable (DataPort).
        """
        locator = XMLFileLocator(self.subworkflow)
        vistrail = locator.load()
        version = vistrail.get_latest_version()
        pipeline = vistrail.getPipeline(version)

        inputports = find_modules_by_type(pipeline, [InputPort])
        if not inputports:
            raise ValueError("No InputPort module")

        currentports = {port.name: port for port in self.ports}
        seenports = set()
        for port in inputports:
            name = get_function(port, 'name')
            if not name:
                raise ValueError("Subworkflow of plot '%s' has an InputPort "
                                 "with no name" % self.name)
            if name in seenports:
                raise ValueError("Subworkflow of plot '%s' has several "
                                 "InputPort modules with name '%s'" % (
                                 self.name, name))
            spec = get_function(port, 'spec')
            optional = get_function(port, 'optional')
            if optional == 'True':
                optional = True
            elif optional == 'False':
                optional = False
            else:
                optional = None

            try:
                currentport = currentports[name]
            except KeyError:
                # If the package didn't provide any port, it's ok, we can
                # discover them. But if some were present and some were
                # forgotten, emit a warning
                if currentports:
                    warnings.warn("Declaration of plot '%s' omitted port "
                                  "'%s'" % (self.name, name))
                if not spec:
                    warnings.warn("Subworkflow of plot '%s' has an InputPort "
                                  "'%s' with no type -- assuming Module" % (
                                  self.name, name))
                    spec = 'edu.utah.sci.vistrails.basic:Module'
                if not optional:
                    optional = False
                type = resolve_descriptor(spec, package_identifier)
                if issubclass(type.module, Constant):
                    self.ports.append(InputPort(
                            name=name,
                            type=type,
                            optional=optional))
                else:
                    self.ports.append(DataPort(
                            name=name,
                            type=type,
                            optional=optional))
            else:
                currentspec = (currentport.type.identifier +
                               ':' +
                               currentport.type.name)
                if ((spec and spec != currentspec) or
                        (optional is not None and
                         optional != currentport.optional)):
                    warnings.warn("Declaration of port '%s' from plot '%s' "
                                  "differs from subworkflow contents" % (
                                  name, self.name))
            seenports.add(name)

        # If the package declared ports that we didn't see
        missingports = list(set(currentports.keys()) - seenports)
        if currentports and missingports:
            raise ValueError("Declaration of plot '%s' mentions missing "
                             "InputPort module '%s'" % (
                             self.name, missingports[0]))

        for port in self.ports:
            if isinstance(port, ConstantPort):
                module = port.type.module
                port.widget_class = get_widget_class(module)


class VariableOperation(object):
    """An operation descriptor.

    Describes a variable operation. These objects should be created by a
    VisTrails package for each operation it wants to register with DAT, and
    added to a global '_variable_operations' list in the 'init' module (for a
    reloadable package).

    name is mandatory and is what will need to be typed to call the operation.
    It can also be an operator: +, -, *, /
    callback is a function that will be called to construct the new variable
    from the operands.
    args is a tuple; each element is the type (or types) accepted for that
    parameter. For instance, an operation that accepts two arguments, the first
    argument being a String and the second argument either a Float or an
    Integer, use: args=(String, (Float, Integer))
    symmetric means that the function will be called if the arguments are
    backwards; this only works for operations with 2 arguments of different
    types. It is useful for operators such as * and +.
    """
    def __init__(self, name, args, return_type,
             callback=None, subworkflow=None, symmetric=False):
        self.name = name
        self.parameters = args
        self.return_type = return_type
        self.callback = self.subworkflow = None
        if callback is not None and subworkflow is not None:
            raise ValueError("VariableOperation() got both callback and "
                             "subworkflow parameters")
        elif callback is not None:
            self.callback = callback
        elif subworkflow is not None:
            caller = inspect.currentframe().f_back
            package = os.path.dirname(inspect.getabsfile(caller))
            self.subworkflow = subworkflow.format(package_dir=package)
        else:
            raise ValueError("VariableOperation() got neither callback nor "
                             "subworkflow parameters")
        self.symmetric = symmetric


class OperationArgument(object):
    """One of the argument of an operation.

    Describes one of the arguments of a VariableOperation. These objects should
    be created by a VisTrails package and passed in a list as the 'args'
    argument of VariableOperation's constructor.

    name is mandatory and is what will be passed to the callback function or
    subworkflow. Note that arguments are passed as keywords, not positional
    arguments.
    types is a VisTrails Module subclass, or a sequence of Module subclasses,
    in which case the argument will accept any of these types.
    """
    def __init__(self, name, types):
        self.name = name
        if isinstance(types, (list, tuple)):
            self.types = tuple(types)
        else:
            self.types = (types,)


def get_function(module, function_name):
    """Get the value of a function of a pipeline module.
    """
    for function in module.functions:
        if function.name == function_name:
            if len(function.params) > 0:
                return function.params[0].strValue
    return None


def delete_linked(controller, modules, operations,
                  module_filter=lambda m: True,
                  connection_filter=lambda c: True,
                  depth=sys.maxint):
    """Delete all modules and connections linked to the specified modules.

    module_filter is an optional function called during propagation to modules.

    connection_filter is an optional function called during propagation to
    connections.

    depth_limit is an optional integer limiting the depth of the operation.
    """
    # Build a map of the connections in which each module takes part
    module_connections = dict()
    for connection in controller.current_pipeline.connection_list:
        for mod in (connection.source.moduleId,
                    connection.destination.moduleId):
            conns = module_connections.setdefault(mod, set())
            conns.add(connection)

    visited_connections = set()

    if isinstance(modules, (list, tuple, set)):
        open_list = modules
    else:
        open_list = [modules]
    to_delete = set(module for module in open_list)

    # At each step
    while depth > 0 and open_list:
        new_open_list = []
        # For each module considered
        for module in open_list:
            # For each connection it takes part in
            for connection in module_connections.get(module.id, []):
                # If that connection passes the filter
                if (connection not in visited_connections and
                        connection_filter(connection)):
                    # Get the other module
                    if connection.source.moduleId == module.id:
                        other_mod = connection.destination.moduleId
                    else:
                        other_mod = connection.source.moduleId
                    other_mod = controller.current_pipeline.modules[other_mod]
                    if other_mod in to_delete:
                        continue
                    # And if it passes the filter
                    if module_filter(other_mod):
                        # Remove it
                        to_delete.add(other_mod)
                        # And add it to the list
                        new_open_list.append(other_mod)
                visited_connections.add(connection)

        open_list = new_open_list
        depth -= 1

    conn_to_delete = set()
    for module in to_delete:
        conn_to_delete.update(module_connections.get(module.id, []))
    operations.extend(('delete', conn) for conn in conn_to_delete)
    operations.extend(('delete', module) for module in to_delete)

    return set(mod.id for mod in to_delete)


def find_modules_by_type(pipeline, moduletypes):
    """Finds all modules that subclass one of the given types in the pipeline.
    """
    moduletypes = tuple(moduletypes)
    result = []
    for module in pipeline.module_list:
        desc = module.module_descriptor
        if issubclass(desc.module, moduletypes):
            result.append(module)
    return result


def get_pipeline_location(controller, pipelineInfo):
    pipeline = controller.vistrail.getPipeline(pipelineInfo.version)

    location_modules = find_modules_by_type(pipeline, [CellLocation])
    if len(location_modules) == 1:
        loc = location_modules[0]
        row = int(get_function(loc, 'Row')) - 1
        col = int(get_function(loc, 'Column')) - 1
        return row, col
    raise ValueError


class PipelineGenerator(object):
    """A wrapper for simple operations that keeps a list of all modules.

    This wraps simple operations on the pipeline and keeps the list of
    VisTrails ops internally. It also keeps a list of all modules needed by
    VisTrails's layout function.
    """
    def __init__(self, controller):
        self.controller = controller
        self.operations = []
        self.all_modules = set(controller.current_pipeline.module_list)
        self.all_connections = set(controller.current_pipeline.connection_list)

    def append_operations(self, operations):
        for op in operations:
            if op[0] == 'add':
                if isinstance(op[1], PipelineModule):
                    self.all_modules.add(op[1])
                elif isinstance(op[1], Connection):
                    self.all_connections.add(op[1])
        self.operations.extend(operations)

    def copy_module(self, module):
        """Copy a VisTrails module to this controller.

        Returns the new module (that is not yet created in the vistrail!)
        """
        module = module.do_copy(True, self.controller.vistrail.idScope, {})
        self.operations.append(('add', module))
        self.all_modules.add(module)
        return module

    def add_module(self, module):
        self.operations.append(('add', module))
        self.all_modules.add(module)

    def connect_modules(self, src_mod, src_port, dest_mod, dest_port):
        new_conn = self.controller.create_connection(
                src_mod, src_port,
                dest_mod, dest_port)
        self.operations.append(('add', new_conn))
        self.all_connections.add(new_conn)
        return new_conn.id

    def update_function(self, module, portname, values):
        self.operations.extend(self.controller.update_function_ops(
                module, portname, values))

    def delete_linked(self, modules, **kwargs):
        """Wrapper for delete_linked().

        This calls delete_linked with the controller and list of operations,
        and updates the internal list of all modules to be layout.
        """
        deleted_ids = delete_linked(
                self.controller, modules, self.operations, **kwargs)
        self.all_modules = set(
                m
                for m in self.all_modules
                if m.id not in deleted_ids)
        self.all_connections = set(
                c
                for c in self.all_connections
                if (c.source.moduleId not in deleted_ids and
                        c.destination.moduleId not in deleted_ids))

    def delete_modules(self, modules):
        self.delete_linked(modules, depth=0)

    def perform_action(self):
        """Layout all the modules and create the action.
        """
        def get_visible_ports(port_list, visible_ports):
            output_list = []
            visible_list = []
            for p in port_list:
                if not p.optional:
                    output_list.append(p)
                elif p.name in visible_ports:
                    visible_list.append(p)
            output_list.extend(visible_list)
            return output_list

        wf = LayoutPipeline()
        wf_iport_map = {}
        wf_oport_map = {}

        for module in self.all_modules:
            wf_mod = wf.createModule(
                    module.id, module.name,
                    len(module.destinationPorts()),
                    len(module.sourcePorts()))
            wf_mod._actual_module = module
            input_ports = get_visible_ports(module.destinationPorts(),
                                            module.visible_input_ports)
            output_ports = get_visible_ports(module.sourcePorts(),
                                             module.visible_output_ports)

            for i, p in enumerate(input_ports):
                if module.id not in wf_iport_map:
                    wf_iport_map[module.id] = {}
                wf_iport_map[module.id][p.name] = wf_mod.input_ports[i]
            for i, p in enumerate(output_ports):
                if module.id not in wf_oport_map:
                    wf_oport_map[module.id] = {}
                wf_oport_map[module.id][p.name] = wf_mod.output_ports[i]

        for conn in self.all_connections:
            src = wf_oport_map[conn.sourceId][conn.source.name]
            dst = wf_iport_map[conn.destinationId][conn.destination.name]
            wf.createConnection(src.module, src.index, 
                                dst.module, dst.index)

        def get_module_size(m):
            return 130, 50 # TODO-dat : Of course, this is wrong

        layout = WorkflowLayout(
                wf,
                get_module_size,
                CurrentTheme.MODULE_PORT_MARGIN,
                (CurrentTheme.PORT_WIDTH,  CurrentTheme.PORT_HEIGHT),
                CurrentTheme.MODULE_PORT_SPACE)
        layout.compute_module_sizes()
        layout.assign_modules_to_layers()
        layout.assign_module_permutation_to_each_layer()
        layer_x_separation = layer_y_separation = 50
        layout.compute_layout(layer_x_separation, layer_y_separation)

        center_out = [0.0, 0.0]
        for wf_mod in wf.modules:
            center_out[0] += wf_mod.layout_pos.x
            center_out[1] += wf_mod.layout_pos.y
        center_out[0] /= float(len(self.all_modules))
        center_out[1] /= float(len(self.all_modules))

        pipeline = self.controller.current_pipeline
        existing_modules = set(pipeline.module_list)
        for wf_mod in wf.modules:
            module = wf_mod._actual_module
            x = wf_mod.layout_pos.x - center_out[0]
            y = wf_mod.layout_pos.y - center_out[1]
            y = -y # Yes, it's backwards in VisTrails
            if module in existing_modules:
                # This module already exists in the workflow, we have to emit a
                # move operation
                # See vistrails.core.vistrail.controller:
                #         VistrailController#move_module_list()
                loc_id = self.controller.vistrail.idScope.getNewId(
                        Location.vtType)
                location = Location(id=loc_id, x=x, y=y)
                if module.location and module.location.id != -1:
                    old_location = module.location
                    self.operations.append(('change', old_location, location,
                                            module.vtType, module.id))
                else:
                    self.operations.append(('add', location,
                                            module.vtType, module.id))
            else:
                # This module's addition to the workflow is pending, as
                # create_action() was not yet called
                # We can just change its position
                module.location.x = x
                module.location.y = y

        action = create_action(self.operations)
        self.controller.add_new_action(action)
        return self.controller.perform_action(action)


def add_variable_subworkflow(generator, variable, plot_ports=None):
    """Add a variable subworkflow to the pipeline.

    Copy the variable subworkflow from its own pipeline to the given one.

    If plot_ports is given, connects the pipeline to the ports in plot_ports,
    and returns the ids of the connections tying this variable to the plot,
    which are used to build the pipeline's conn_map.

    If plot_ports is None, just returns the (module, port_name) of the output
    port.
    """
    if isinstance(variable, Pipeline):
        var_pipeline = variable
    else:
        var_pipeline = generator.controller.vistrail.getPipeline(
                'dat-var-%s' % variable)

    reg = get_module_registry()
    outputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'OutputPort')

    # Copy every module but the OutputPort
    output_id = None
    var_modules_map = dict() # old_mod_id -> new_module
    for module in var_pipeline.modules.itervalues():
        if (module.module_descriptor is outputport_desc and
                get_function(module, 'name') == 'value'):
            output_id = module.id
        else:
            # We can't just add this module to the new pipeline!
            # We need to create a new one to avoid id collisions
            var_modules_map[module.id] = generator.copy_module(module)

    if output_id is None:
        raise ValueError("add_variable_subworkflow: variable pipeline has no "
                         "'OutputPort' module")

    # Copy every connection except the one to the OutputPort module
    for connection in var_pipeline.connection_list:
        if connection.destination.moduleId != output_id:
            generator.connect_modules(
                    var_modules_map[connection.source.moduleId],
                    connection.source.name,
                    var_modules_map[connection.destination.moduleId],
                    connection.destination.name)

    if plot_ports:
        connection_ids = []
        # Connects the port previously connected to the OutputPort to the ports
        # in plot_ports
        for connection in var_pipeline.connection_list:
            if connection.destination.moduleId == output_id:
                for var_output_mod, var_output_port in plot_ports:
                    connection_ids.append(generator.connect_modules(
                            var_modules_map[connection.source.moduleId],
                            connection.source.name,
                            var_output_mod,
                            var_output_port))
        return connection_ids
    else:
        # Just find the output port and return it
        for connection in var_pipeline.connection_list:
            if connection.destination.moduleId == output_id:
                return (var_modules_map[connection.source.moduleId],
                        connection.source.name)
        assert False


def add_variable_subworkflow_typecast(generator, variable, plot_ports,
                                       expected_type, typecast):
    if issubclass(variable.type.module, expected_type.module):
        return add_variable_subworkflow(
                generator,
                variable.name,
                plot_ports)
    else:
        # Load the variable from the workflow
        var_pipeline = Variable.from_pipeline(
                generator.controller, variable.name)

        # Apply the operation
        var_pipeline = typecast(
                generator.controller, var_pipeline,
                variable.type, expected_type)

        generator.append_operations(var_pipeline._generator.operations)
        if plot_ports:
            connection_ids = []
            for var_output_mod, var_output_port in plot_ports:
                connection_ids.append(generator.connect_modules(
                        var_pipeline._output_module,
                        var_pipeline._outputport_name,
                        var_output_mod,
                        var_output_port))
            return connection_ids
        else:
            return (var_pipeline._output_module, var_pipeline._outputport_name)


def add_constant_module(generator, descriptor, constant, plot_ports):
    module = generator.controller.create_module_from_descriptor(descriptor)
    generator.add_module(module)
    generator.update_function(module, 'value', [constant])

    connection_ids = []
    for output_mod, output_port in plot_ports:
        connection_ids.append(generator.connect_modules(
                module,
                'value',
                output_mod,
                output_port))

    return connection_ids


def create_pipeline(controller, recipe, cell_info, typecast=None):
    """Create a pipeline from a recipe and return its information.
    """
    # Build from the root version
    controller.change_selected_version(0)

    reg = get_module_registry()

    generator = PipelineGenerator(controller)

    inputport_desc = reg.get_descriptor_by_name(
            'edu.utah.sci.vistrails.basic', 'InputPort')

    # Add the plot subworkflow
    locator = XMLFileLocator(recipe.plot.subworkflow)
    vistrail = locator.load()
    version = vistrail.get_latest_version()
    plot_pipeline = vistrail.getPipeline(version)

    # Copy every module but the InputPorts
    plot_modules_map = dict() # old module id -> new module
    for module in plot_pipeline.modules.itervalues():
        if module.module_descriptor is not inputport_desc:
            plot_modules_map[module.id] = generator.copy_module(module)

    def _get_or_create_module(moduleType):
        """Returns or creates a new module of the given type.

        Warns if multiple modules of that type were found.
        """
        modules = find_modules_by_type(plot_pipeline, [moduleType])
        if not modules:
            desc = reg.get_descriptor_from_module(moduleType)
            module = controller.create_module_from_descriptor(desc)
            generator.add_module(module)
            return module, True
        else:
            # Currently we do not support multiple cell locations in one
            # pipeline but this may be a feature in the future, to have
            # linked visualizations in multiple cells
            if len(modules) > 1:
                warnings.warn("Found multiple %s modules in plot "
                              "subworkflow, only using one." % moduleType)
            return plot_modules_map[modules[0].id], False

    # Connect the CellLocation to the SpreadsheetCell
    cell_modules = find_modules_by_type(plot_pipeline,
                                        [SpreadsheetCell])
    if cell_modules:
        # Add a CellLocation module if the plot subworkflow didn't contain one
        location_module, new_location = _get_or_create_module(CellLocation)

        if new_location:
            # Connect the CellLocation to the SpreadsheetCell
            cell_module = plot_modules_map[cell_modules[0].id]
            generator.connect_modules(
                    location_module, 'self',
                    cell_module, 'Location')

        if location_module:
            row, col = cell_info.row, cell_info.column
            generator.update_function(
                    location_module, 'Row', [row + 1])
            generator.update_function(
                    location_module, 'Column', [col + 1])

            if len(cell_modules) > 1:
                warnings.warn("Plot subworkflow '%s' contains more than "
                              "one spreadsheet cell module. Only one "
                              "was connected to a location module." %
                              recipe.plot.name)
    else:
        warnings.warn("Plot subworkflow '%s' does not contain a "
                      "spreadsheet cell module" % recipe.plot.name)

    # Copy the connections and locate the input ports
    plot_params = dict() # param name -> [(module, input port name)]
    for connection in plot_pipeline.connection_list:
        src = plot_pipeline.modules[connection.source.moduleId]
        if src.module_descriptor is inputport_desc:
            param = get_function(src, 'name')
            ports = plot_params.setdefault(param, [])
            ports.append((
                    plot_modules_map[connection.destination.moduleId],
                    connection.destination.name))
        else:
            generator.connect_modules(
                    plot_modules_map[connection.source.moduleId],
                    connection.source.name,
                    plot_modules_map[connection.destination.moduleId],
                    connection.destination.name)

    # Maps a port name to the list of parameters
    # for each parameter, we have a list of connections tying it to modules of
    # the plot
    conn_map = dict() # param: str -> [[conn_id: int]]

    name_to_port = {port.name: port for port in recipe.plot.ports}

    for port_name, parameters in recipe.parameters.iteritems():
        plot_ports = plot_params.get(port_name, [])
        p_conns = conn_map[port_name] = []
        for parameter in parameters:
            if parameter.type == RecipeParameterValue.VARIABLE:
                p_conns.append(add_variable_subworkflow_typecast(
                        generator,
                        parameter.variable,
                        plot_ports,
                        name_to_port[port_name].type,
                        typecast=typecast))
            else: # parameter.type == RecipeParameterValue.CONSTANT
                desc = name_to_port[port_name].type
                p_conns.append(add_constant_module(
                        generator,
                        desc,
                        parameter.constant,
                        plot_ports))

    if controller.current_version != 0:
        controller.change_selected_version(0)
    pipeline_version = generator.perform_action()
    controller.vistrail.change_description(
            "Created DAT plot %s" % recipe.plot.name,
            pipeline_version)
    # FIXME : from_root seems to be necessary here, I don't know why
    controller.change_selected_version(pipeline_version, from_root=True)

    # Convert the modules to module ids in the port_map
    port_map = dict()
    for param, portlist in plot_params.iteritems():
        port_map[param] = [(module.id, port) for module, port in portlist]

    return PipelineInformation(pipeline_version, recipe, conn_map, port_map)


class UpdateError(ValueError):
    """Error while updating a pipeline.

    This is recoverable by creating a new pipeline from scratch instead. It can
    be caused by the alteration of the data stored in annotations, or by
    changes in the VisTrails package's code.
    """


def update_pipeline(controller, pipelineInfo, new_recipe, typecast=None):
    """Update a pipeline to a new recipe.

    This takes a similar pipeline and turns it into the new recipe by adding/
    removing/replacing the variable subworkflows.

    It will raise UpdateError if it can't be done; in this case
    create_pipeline() should be considered.
    """
    # Retrieve the pipeline
    controller.change_selected_version(pipelineInfo.version)
    pipeline = controller.current_pipeline
    old_recipe = pipelineInfo.recipe

    # The plots have to be the same
    if old_recipe.plot != new_recipe.plot:
        raise UpdateError("update_pipeline cannot change plot type!")

    generator = PipelineGenerator(controller)

    conn_map = dict()

    # Used to build the description
    added_params = []
    removed_params = []

    name_to_port = {port.name: port for port in new_recipe.plot.ports}

    for port_name in (set(old_recipe.parameters.iterkeys()) |
                             set(new_recipe.parameters.iterkeys())):
        # param -> [[conn_id]]
        old_params = dict()
        for i, param in enumerate(old_recipe.parameters.get(port_name, [])):
            conns = old_params.setdefault(param, [])
            conns.append(list(pipelineInfo.conn_map[port_name][i]))
        new_params = list(new_recipe.parameters.get(port_name, []))
        conn_lists = conn_map.setdefault(port_name, [])

        # Loop on new parameters
        for param in new_params:
            # Remove one from old_params
            old = old_params.get(param)
            if old:
                old_conns = old.pop(0)
                if not old:
                    del old_params[param]

                conn_lists.append(old_conns)
                continue

            # Can't remove, meaning that there is more of this param than there
            # was before
            # Add this param on this port
            plot_ports = [(pipeline.modules[mod_id], port)
                          for mod_id, port in (
                                  pipelineInfo.port_map[port_name])]
            if param.type == RecipeParameterValue.VARIABLE:
                conn_lists.append(add_variable_subworkflow_typecast(
                        generator,
                        param.variable,
                        plot_ports,
                        name_to_port[port_name].type,
                        typecast=typecast))
            else: #param.type == RecipeParameterValue.CONSTANT:
                desc = name_to_port[port_name].type
                conn_lists.append(add_constant_module(
                        generator,
                        desc,
                        param.constant,
                        plot_ports))

            added_params.append(port_name)

        # Now loop on the remaining old parameters
        # If they haven't been removed by the previous loop, that means that
        # there were more of them in the old recipe
        for conn_lists in old_params.itervalues():
            for connections in conn_lists:
                connections = set(pipeline.connections[c]
                                  for c in connections)

                # Remove the variable subworkflow
                modules = set(pipeline.modules[c.source.moduleId]
                              for c in connections)
                generator.delete_linked(
                        modules,
                        connection_filter=lambda c: c not in connections)

                removed_params.append(port_name)

    # We didn't find anything to change
    if not (added_params or removed_params):
        return pipelineInfo

    if controller.current_version != pipelineInfo.version:
        controller.change_selected_version(pipelineInfo.version)
    pipeline_version = generator.perform_action()

    controller.vistrail.change_description(
            describe_dat_update(added_params, removed_params),
            pipeline_version)

    controller.change_selected_version(pipeline_version, from_root=True)

    return PipelineInformation(pipeline_version, new_recipe,
                               conn_map, pipelineInfo.port_map)


def describe_dat_update(added_params, removed_params):
    # We only added parameters
    if added_params and not removed_params:
        # We added one
        if len(added_params) == 1:
            return "Added DAT parameter to %s" % added_params[0]
        # We added several, but all on the same port
        elif all(param == added_params[0]
                 for param in added_params[1:]):
            return "Added DAT parameters to %s" % added_params[0]
        # We added several on different ports
        else:
            return "Added DAT parameters"
    # We only removed parameters
    elif removed_params and not added_params:
        # We removed one
        if len(removed_params) == 1:
            return "Removed DAT parameter from %s" % (
                    removed_params[0])
        # We removed several, but all on the same port
        elif all(param == removed_params[0]
                 for param in removed_params[1:]):
            return "Removed DAT parameters from %s" % (
                    removed_params[0])
        # We removed several from different ports
        else:
            return "Removed DAT parameters"
    # Both additions and deletions
    else:
        # Replaced a parameter
        if ((len(added_params), len(removed_params)) == (1, 1) and
                added_params[0] == removed_params[0]):
            return "Changed DAT parameter on %s" % added_params[0]
        # Did all kind of stuff
        else:
            if added_params:
                port = added_params[0]
            else:
                port = removed_params[0]
            # ... to a single port
            if all(param == port
                   for param in chain(added_params, removed_params)):
                return "Changed DAT parameters on %s" % port
            # ... to different ports
            else:
                return "Changed DAT parameters"


# We don't use vistrails.packages.spreadsheet.spreadsheet_execute:
# executePipelineWithProgress() because it doesn't update provenance
# We need to use the controller's execute_workflow_list() instead of calling
# the interpreter directly
def executePipeline(controller, pipeline,
        reason, locator, version,
        **kwargs):
    """Execute the pipeline while showing a progress dialog.
    """
    _ = translate('executePipeline')

    totalProgress = len(pipeline.modules)
    progress = QtGui.QProgressDialog(_("Executing..."),
                                     QtCore.QString(),
                                     0, totalProgress)
    progress.setWindowTitle(_("Pipeline Execution"))
    progress.setWindowModality(QtCore.Qt.WindowModal)
    progress.show()
    def moduleExecuted(objId):
        progress.setValue(progress.value()+1)
        QtCore.QCoreApplication.processEvents()
    if kwargs.has_key('module_executed_hook'):
        kwargs['module_executed_hook'].append(moduleExecuted)
    else:
        kwargs['module_executed_hook'] = [moduleExecuted]

    controller.execute_workflow_list([(
            locator,        # locator
            version,        # version
            pipeline,       # pipeline
            DummyView(),    # view
            None,           # custom_aliases
            None,           # custom_params
            reason,         # reason
            kwargs)])       # extra_info
    get_vistrails_application().send_notification('execution_updated')
    progress.setValue(totalProgress)
    progress.hide()
    progress.deleteLater()


def try_execute(controller, pipelineInfo, sheetname, recipe=None):
    if recipe is None:
        recipe = pipelineInfo.recipe

    if all(
            port.optional or recipe.parameters.has_key(port.name)
            for port in recipe.plot.ports):
        # Create a copy of that pipeline so we can change it
        controller.change_selected_version(pipelineInfo.version)
        pipeline = controller.current_pipeline
        pipeline = copy.copy(pipeline)

        # Add the SheetReference to the pipeline
        modules = find_modules_by_type(pipeline, [CellLocation])

        # Hack copied from spreadsheet_execute
        create_module = VistrailController.create_module_static
        create_function = VistrailController.create_function_static
        create_connection = VistrailController.create_connection_static
        id_scope = pipeline.tmp_id
        orig_getNewId = pipeline.tmp_id.__class__.getNewId
        def getNewId(self, objType):
            return -orig_getNewId(self, objType)
        pipeline.tmp_id.__class__.getNewId = getNewId
        try:
            for module in modules:
                # Remove all SheetReference connected to this CellLocation
                conns_to_delete = []
                for conn_id, conn in pipeline.connections.iteritems():
                    if (conn.destinationId == module.id and
                            pipeline.modules[conn.sourceId] is SheetReference):
                        conns_to_delete.append(conn_id)
                for conn_id in conns_to_delete:
                    pipeline.delete_connection(conn_id)

                # Add the SheetReference module
                sheet_module = create_module(
                        id_scope,
                        'edu.utah.sci.vistrails.spreadsheet',
                        'SheetReference')
                sheet_name = create_function(id_scope, sheet_module,
                                             'SheetName', [str(sheetname)])
                sheet_module.add_function(sheet_name)

                # Connect with the CellLocation
                conn = create_connection(id_scope,
                                         sheet_module, 'self',
                                         module, 'SheetReference')

                pipeline.add_module(sheet_module)
                pipeline.add_connection(conn)
        finally:
            pipeline.tmp_id.__class__.getNewId = orig_getNewId

        # Execute the new pipeline
        executePipeline(
                controller,
                pipeline,
                reason="DAT recipe execution",
                locator=controller.locator,
                version=pipelineInfo.version)
        return True
    else:
        return False
