# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from UM.Workspace.WorkspaceReader import WorkspaceReader
from UM.Application import Application

from UM.Logger import Logger
from UM.i18n import i18nCatalog
from UM.Settings.ContainerStack import ContainerStack
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.InstanceContainer import InstanceContainer
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.MimeTypeDatabase import MimeTypeDatabase
from UM.Job import Job
from UM.Preferences import Preferences
from UM.Util import parseBool
from .WorkspaceDialog import WorkspaceDialog

import xml.etree.ElementTree as ET

from cura.Settings.CuraStackBuilder import CuraStackBuilder
from cura.Settings.ExtruderManager import ExtruderManager
from cura.Settings.ExtruderStack import ExtruderStack
from cura.Settings.GlobalStack import GlobalStack
from cura.Settings.CuraContainerStack import _ContainerIndexes
from cura.QualityManager import QualityManager

from configparser import ConfigParser
import zipfile
import io
import configparser
import os

i18n_catalog = i18nCatalog("cura")


##    Base implementation for reading 3MF workspace files.
class ThreeMFWorkspaceReader(WorkspaceReader):
    def __init__(self):
        super().__init__()
        self._supported_extensions = [".3mf"]
        self._dialog = WorkspaceDialog()
        self._3mf_mesh_reader = None
        self._container_registry = ContainerRegistry.getInstance()

        # suffixes registered with the MineTypes don't start with a dot '.'
        self._definition_container_suffix = "." + ContainerRegistry.getMimeTypeForContainer(DefinitionContainer).preferredSuffix
        self._material_container_suffix = None # We have to wait until all other plugins are loaded before we can set it
        self._instance_container_suffix = "." + ContainerRegistry.getMimeTypeForContainer(InstanceContainer).preferredSuffix
        self._container_stack_suffix = "." + ContainerRegistry.getMimeTypeForContainer(ContainerStack).preferredSuffix
        self._extruder_stack_suffix = "." + ContainerRegistry.getMimeTypeForContainer(ExtruderStack).preferredSuffix
        self._global_stack_suffix = "." + ContainerRegistry.getMimeTypeForContainer(GlobalStack).preferredSuffix

        # Certain instance container types are ignored because we make the assumption that only we make those types
        # of containers. They are:
        #  - quality
        #  - variant
        self._ignored_instance_container_types = {"quality", "variant"}

        self._resolve_strategies = {}

        self._id_mapping = {}

        # In Cura 2.5 and 2.6, the empty profiles used to have those long names
        self._old_empty_profile_id_dict = {"empty_%s" % k: "empty" for k in ["material", "variant"]}

    ##  Get a unique name based on the old_id. This is different from directly calling the registry in that it caches results.
    #   This has nothing to do with speed, but with getting consistent new naming for instances & objects.
    def getNewId(self, old_id):
        if old_id not in self._id_mapping:
            self._id_mapping[old_id] = self._container_registry.uniqueName(old_id)
        return self._id_mapping[old_id]

    ##  Separates the given file list into a list of GlobalStack files and a list of ExtruderStack files.
    #
    #   In old versions, extruder stack files have the same suffix as container stack files ".stack.cfg".
    #
    def _determineGlobalAndExtruderStackFiles(self, project_file_name, file_list):
        archive = zipfile.ZipFile(project_file_name, "r")

        global_stack_file_list = [name for name in file_list if name.endswith(self._global_stack_suffix)]
        extruder_stack_file_list = [name for name in file_list if name.endswith(self._extruder_stack_suffix)]

        # separate container stack files and extruder stack files
        files_to_determine = [name for name in file_list if name.endswith(self._container_stack_suffix)]
        for file_name in files_to_determine:
            # FIXME: HACK!
            # We need to know the type of the stack file, but we can only know it if we deserialize it.
            # The default ContainerStack.deserialize() will connect signals, which is not desired in this case.
            # Since we know that the stack files are INI files, so we directly use the ConfigParser to parse them.
            serialized = archive.open(file_name).read().decode("utf-8")
            stack_config = ConfigParser()
            stack_config.read_string(serialized)

            # sanity check
            if not stack_config.has_option("metadata", "type"):
                Logger.log("e", "%s in %s doesn't seem to be valid stack file", file_name, project_file_name)
                continue

            stack_type = stack_config.get("metadata", "type")
            if stack_type == "extruder_train":
                extruder_stack_file_list.append(file_name)
            elif stack_type == "machine":
                global_stack_file_list.append(file_name)
            else:
                Logger.log("w", "Unknown container stack type '%s' from %s in %s",
                           stack_type, file_name, project_file_name)

        if len(global_stack_file_list) != 1:
            raise RuntimeError("More than one global stack file found: [%s]" % str(global_stack_file_list))

        return global_stack_file_list[0], extruder_stack_file_list

    ##  read some info so we can make decisions
    #   \param file_name
    #   \param show_dialog  In case we use preRead() to check if a file is a valid project file, we don't want to show a dialog.
    def preRead(self, file_name, show_dialog=True, *args, **kwargs):
        self._3mf_mesh_reader = Application.getInstance().getMeshFileHandler().getReaderForFile(file_name)
        if self._3mf_mesh_reader and self._3mf_mesh_reader.preRead(file_name) == WorkspaceReader.PreReadResult.accepted:
            pass
        else:
            Logger.log("w", "Could not find reader that was able to read the scene data for 3MF workspace")
            return WorkspaceReader.PreReadResult.failed

        machine_type = ""
        variant_type_name = i18n_catalog.i18nc("@label", "Nozzle")

        # Check if there are any conflicts, so we can ask the user.
        archive = zipfile.ZipFile(file_name, "r")
        cura_file_names = [name for name in archive.namelist() if name.startswith("Cura/")]

        # A few lists of containers in this project files.
        # When loading the global stack file, it may be associated with those containers, which may or may not be
        # in Cura already, so we need to provide them as alternative search lists.
        instance_container_list = []

        resolve_strategy_keys = ["machine", "material", "quality_changes"]
        self._resolve_strategies = {k: None for k in resolve_strategy_keys}
        containers_found_dict = {k: False for k in resolve_strategy_keys}

        #
        # Read definition containers
        #
        machine_definition_container_count = 0
        extruder_definition_container_count = 0
        definition_container_files = [name for name in cura_file_names if name.endswith(self._definition_container_suffix)]
        for each_definition_container_file in definition_container_files:
            container_id = self._stripFileToId(each_definition_container_file)
            definitions = self._container_registry.findDefinitionContainersMetadata(id = container_id)

            if not definitions:
                definition_container = DefinitionContainer(container_id)
                definition_container.deserialize(archive.open(each_definition_container_file).read().decode("utf-8"), file_name = each_definition_container_file)
                definition_container = definition_container.getMetaData()

            else:
                definition_container = definitions[0]

            definition_container_type = definition_container.get("type")
            if definition_container_type == "machine":
                machine_type = definition_container["name"]
                variant_type_name = definition_container.get("variants_name", variant_type_name)

                machine_definition_container_count += 1
            elif definition_container_type == "extruder":
                extruder_definition_container_count += 1
            else:
                Logger.log("w", "Unknown definition container type %s for %s",
                           definition_container_type, each_definition_container_file)
            Job.yieldThread()
        # sanity check
        if machine_definition_container_count != 1:
            msg = "Expecting one machine definition container but got %s" % machine_definition_container_count
            Logger.log("e", msg)
            raise RuntimeError(msg)

        material_labels = []
        material_conflict = False
        xml_material_profile = self._getXmlProfileClass()
        if self._material_container_suffix is None:
            self._material_container_suffix = ContainerRegistry.getMimeTypeForContainer(xml_material_profile).preferredSuffix
        if xml_material_profile:
            material_container_files = [name for name in cura_file_names if name.endswith(self._material_container_suffix)]
            for material_container_file in material_container_files:
                container_id = self._stripFileToId(material_container_file)
                material_labels.append(self._getMaterialLabelFromSerialized(archive.open(material_container_file).read().decode("utf-8")))
                if self._container_registry.findContainersMetadata(id = container_id): #This material already exists.
                    containers_found_dict["material"] = True
                    if not self._container_registry.isReadOnly(container_id):  # Only non readonly materials can be in conflict
                        material_conflict = True
                Job.yieldThread()

        # Check if any quality_changes instance container is in conflict.
        instance_container_files = [name for name in cura_file_names if name.endswith(self._instance_container_suffix)]
        quality_name = ""
        quality_type = ""
        num_settings_overriden_by_quality_changes = 0 # How many settings are changed by the quality changes
        num_settings_overriden_by_definition_changes = 0 # How many settings are changed by the definition changes
        num_user_settings = 0
        quality_changes_conflict = False
        definition_changes_conflict = False

        for each_instance_container_file in instance_container_files:
            container_id = self._stripFileToId(each_instance_container_file)
            instance_container = InstanceContainer(container_id)

            # Deserialize InstanceContainer by converting read data from bytes to string
            instance_container.deserialize(archive.open(each_instance_container_file).read().decode("utf-8"),
                                           file_name = each_instance_container_file)
            instance_container_list.append(instance_container)

            container_type = instance_container.getMetaDataEntry("type")
            if container_type == "quality_changes":
                quality_name = instance_container.getName()
                num_settings_overriden_by_quality_changes += len(instance_container._instances)
                # Check if quality changes already exists.
                quality_changes = self._container_registry.findInstanceContainers(id = container_id)
                if quality_changes:
                    containers_found_dict["quality_changes"] = True
                    # Check if there really is a conflict by comparing the values
                    if quality_changes[0] != instance_container:
                        quality_changes_conflict = True
            elif container_type == "definition_changes":
                definition_name = instance_container.getName()
                num_settings_overriden_by_definition_changes += len(instance_container._instances)
                # Check if definition changes already exists.
                definition_changes = self._container_registry.findInstanceContainers(id = container_id)
                # Check if there is any difference the loaded settings from the project file and the settings in Cura.
                if definition_changes:
                    containers_found_dict["definition_changes"] = True
                    # Check if there really is a conflict by comparing the values
                    if definition_changes[0] != instance_container:
                        definition_changes_conflict = True
            elif container_type == "quality":
                if not quality_name:
                    quality_name = instance_container.getName()
            elif container_type == "user":
                num_user_settings += len(instance_container._instances)
            elif container_type in self._ignored_instance_container_types:
                # Ignore certain instance container types
                Logger.log("w", "Ignoring instance container [%s] with type [%s]", container_id, container_type)
                continue

            Job.yieldThread()

        # Load ContainerStack files and ExtruderStack files
        global_stack_file, extruder_stack_files = self._determineGlobalAndExtruderStackFiles(
            file_name, cura_file_names)
        machine_conflict = False
        # Because there can be cases as follows:
        #  - the global stack exists but some/all of the extruder stacks DON'T exist
        #  - the global stack DOESN'T exist but some/all of the extruder stacks exist
        # To simplify this, only check if the global stack exists or not
        container_id = self._stripFileToId(global_stack_file)
        serialized = archive.open(global_stack_file).read().decode("utf-8")
        machine_name = self._getMachineNameFromSerializedStack(serialized)
        stacks = self._container_registry.findContainerStacks(id = container_id)
        if stacks:
            global_stack = stacks[0]
            containers_found_dict["machine"] = True
            # Check if there are any changes at all in any of the container stacks.
            id_list = self._getContainerIdListFromSerialized(serialized)
            for index, container_id in enumerate(id_list):
                # take into account the old empty container IDs
                container_id = self._old_empty_profile_id_dict.get(container_id, container_id)
                if global_stack.getContainer(index).getId() != container_id:
                    machine_conflict = True
                    break
        Job.yieldThread()

        # if the global stack is found, we check if there are conflicts in the extruder stacks
        if containers_found_dict["machine"] and not machine_conflict:
            for extruder_stack_file in extruder_stack_files:
                container_id = self._stripFileToId(extruder_stack_file)
                serialized = archive.open(extruder_stack_file).read().decode("utf-8")
                parser = configparser.ConfigParser()
                parser.read_string(serialized)

                # The check should be done for the extruder stack that's associated with the existing global stack,
                # and those extruder stacks may have different IDs.
                # So we check according to the positions

                position = str(parser["metadata"]["position"])
                if position not in global_stack.extruders:
                    # The extruder position defined in the project doesn't exist in this global stack.
                    # We can say that it is a machine conflict, but it is very hard to override the machine in this
                    # case because we need to override the existing extruders and add the non-existing extruders.
                    #
                    # HACK:
                    # To make this simple, we simply say that there is no machine conflict and create a new machine
                    # by default.
                    machine_conflict = False
                    break

                existing_extruder_stack = global_stack.extruders[position]
                # check if there are any changes at all in any of the container stacks.
                id_list = self._getContainerIdListFromSerialized(serialized)
                for index, container_id in enumerate(id_list):
                    # take into account the old empty container IDs
                    container_id = self._old_empty_profile_id_dict.get(container_id, container_id)
                    if existing_extruder_stack.getContainer(index).getId() != container_id:
                        machine_conflict = True
                        break

        num_visible_settings = 0
        has_visible_settings_string = False
        try:
            temp_preferences = Preferences()
            serialized = archive.open("Cura/preferences.cfg").read().decode("utf-8")
            temp_preferences.deserialize(serialized)

            visible_settings_string = temp_preferences.getValue("general/visible_settings")
            has_visible_settings_string = visible_settings_string is not None
            if visible_settings_string is not None:
                num_visible_settings = len(visible_settings_string.split(";"))
            active_mode = temp_preferences.getValue("cura/active_mode")
            if not active_mode:
                active_mode = Preferences.getInstance().getValue("cura/active_mode")
        except KeyError:
            # If there is no preferences file, it's not a workspace, so notify user of failure.
            Logger.log("w", "File %s is not a valid workspace.", file_name)
            return WorkspaceReader.PreReadResult.failed

        # In case we use preRead() to check if a file is a valid project file, we don't want to show a dialog.
        if not show_dialog:
            return WorkspaceReader.PreReadResult.accepted

        # prepare data for the dialog
        num_extruders = extruder_definition_container_count
        if num_extruders == 0:
            num_extruders = 1  # No extruder stacks found, which means there is one extruder

        extruders = num_extruders * [""]

        # Show the dialog, informing the user what is about to happen.
        self._dialog.setMachineConflict(machine_conflict)
        self._dialog.setQualityChangesConflict(quality_changes_conflict)
        self._dialog.setDefinitionChangesConflict(definition_changes_conflict)
        self._dialog.setMaterialConflict(material_conflict)
        self._dialog.setHasVisibleSettingsField(has_visible_settings_string)
        self._dialog.setNumVisibleSettings(num_visible_settings)
        self._dialog.setQualityName(quality_name)
        self._dialog.setQualityType(quality_type)
        self._dialog.setNumSettingsOverridenByQualityChanges(num_settings_overriden_by_quality_changes)
        self._dialog.setNumUserSettings(num_user_settings)
        self._dialog.setActiveMode(active_mode)
        self._dialog.setMachineName(machine_name)
        self._dialog.setMaterialLabels(material_labels)
        self._dialog.setMachineType(machine_type)
        self._dialog.setExtruders(extruders)
        self._dialog.setVariantType(variant_type_name)
        self._dialog.setHasObjectsOnPlate(Application.getInstance().platformActivity)
        self._dialog.show()

        # Block until the dialog is closed.
        self._dialog.waitForClose()

        if self._dialog.getResult() == {}:
            return WorkspaceReader.PreReadResult.cancelled

        self._resolve_strategies = self._dialog.getResult()
        #
        # There can be 3 resolve strategies coming from the dialog:
        #  - new:       create a new container
        #  - override:  override the existing container
        #  - None:      There is no conflict, which means containers with the same IDs may or may not be there already.
        #               If there is an existing container, there is no conflict between them, and default to "override"
        #               If there is no existing container, default to "new"
        #
        # Default values
        for key, strategy in self._resolve_strategies.items():
            if key not in containers_found_dict or strategy is not None:
                continue
            self._resolve_strategies[key] = "override" if containers_found_dict[key] else "new"

        return WorkspaceReader.PreReadResult.accepted

    ## Overrides an ExtruderStack in the given GlobalStack and returns the new ExtruderStack.
    def _overrideExtruderStack(self, global_stack, extruder_file_content, extruder_stack_file):
        # Get extruder position first
        extruder_config = configparser.ConfigParser()
        extruder_config.read_string(extruder_file_content)
        if not extruder_config.has_option("metadata", "position"):
            msg = "Could not find 'metadata/position' in extruder stack file"
            Logger.log("e", "Could not find 'metadata/position' in extruder stack file")
            raise RuntimeError(msg)
        extruder_position = extruder_config.get("metadata", "position")
        try:
            extruder_stack = global_stack.extruders[extruder_position]
        except KeyError:
            Logger.log("w", "Could not find the matching extruder stack to override for position %s", extruder_position)
            return None

        # Override the given extruder stack
        extruder_stack.deserialize(extruder_file_content, file_name = extruder_stack_file)

        # return the new ExtruderStack
        return extruder_stack

    ##  Read the project file
    #   Add all the definitions / materials / quality changes that do not exist yet. Then it loads
    #   all the stacks into the container registry. In some cases it will reuse the container for the global stack.
    #   It handles old style project files containing .stack.cfg as well as new style project files
    #   containing global.cfg / extruder.cfg
    #
    #   \param file_name
    def read(self, file_name):
        archive = zipfile.ZipFile(file_name, "r")

        cura_file_names = [name for name in archive.namelist() if name.startswith("Cura/")]

        # Create a shadow copy of the preferences (we don't want all of the preferences, but we do want to re-use its
        # parsing code.
        temp_preferences = Preferences()
        serialized = archive.open("Cura/preferences.cfg").read().decode("utf-8")
        temp_preferences.deserialize(serialized)

        # Copy a number of settings from the temp preferences to the global
        global_preferences = Preferences.getInstance()

        visible_settings = temp_preferences.getValue("general/visible_settings")
        if visible_settings is None:
            Logger.log("w", "Workspace did not contain visible settings. Leaving visibility unchanged")
        else:
            global_preferences.setValue("general/visible_settings", visible_settings)

        categories_expanded = temp_preferences.getValue("cura/categories_expanded")
        if categories_expanded is None:
            Logger.log("w", "Workspace did not contain expanded categories. Leaving them unchanged")
        else:
            global_preferences.setValue("cura/categories_expanded", categories_expanded)

        Application.getInstance().expandedCategoriesChanged.emit()  # Notify the GUI of the change

        self._id_mapping = {}

        # We don't add containers right away, but wait right until right before the stack serialization.
        # We do this so that if something goes wrong, it's easier to clean up.
        containers_to_add = []

        global_stack_file, extruder_stack_files = self._determineGlobalAndExtruderStackFiles(file_name, cura_file_names)

        global_stack = None
        extruder_stacks = []
        extruder_stacks_added = []
        container_stacks_added = []
        machine_extruder_count = None

        containers_added = []

        global_stack_id_original = self._stripFileToId(global_stack_file)
        global_stack_id_new = global_stack_id_original
        global_stack_name_original = self._getMachineNameFromSerializedStack(archive.open(global_stack_file).read().decode("utf-8"))
        global_stack_name_new = global_stack_name_original
        global_stack_need_rename = False

        extruder_stack_id_map = {}  # new and old ExtruderStack IDs map
        if self._resolve_strategies["machine"] == "new":
            # We need a new id if the id already exists
            if self._container_registry.findContainerStacksMetadata(id = global_stack_id_original):
                global_stack_id_new = self.getNewId(global_stack_id_original)
                global_stack_need_rename = True

            global_stack_name_new = self._container_registry.uniqueName(global_stack_name_original)

            for each_extruder_stack_file in extruder_stack_files:
                old_container_id = self._stripFileToId(each_extruder_stack_file)
                new_container_id = old_container_id
                if self._container_registry.findContainerStacksMetadata(id = old_container_id):
                    # get a new name for this extruder
                    new_container_id = self.getNewId(old_container_id)

                extruder_stack_id_map[old_container_id] = new_container_id

        # TODO: For the moment we use pretty naive existence checking. If the ID is the same, we assume in quite a few
        # TODO: cases that the container loaded is the same (most notable in materials & definitions).
        # TODO: It might be possible that we need to add smarter checking in the future.
        Logger.log("d", "Workspace loading is checking definitions...")
        # Get all the definition files & check if they exist. If not, add them.
        definition_container_files = [name for name in cura_file_names if name.endswith(self._definition_container_suffix)]
        for definition_container_file in definition_container_files:
            container_id = self._stripFileToId(definition_container_file)
            definitions = self._container_registry.findDefinitionContainersMetadata(id = container_id)
            if not definitions:
                definition_container = DefinitionContainer(container_id)
                definition_container.deserialize(archive.open(definition_container_file).read().decode("utf-8"),
                                                 file_name = definition_container_file)
                self._container_registry.addContainer(definition_container)
            Job.yieldThread()

        Logger.log("d", "Workspace loading is checking materials...")
        material_containers = []
        # Get all the material files and check if they exist. If not, add them.
        xml_material_profile = self._getXmlProfileClass()
        if self._material_container_suffix is None:
            self._material_container_suffix = ContainerRegistry.getMimeTypeForContainer(xml_material_profile).suffixes[0]
        if xml_material_profile:
            material_container_files = [name for name in cura_file_names if name.endswith(self._material_container_suffix)]
            for material_container_file in material_container_files:
                container_id = self._stripFileToId(material_container_file)
                materials = self._container_registry.findInstanceContainers(id = container_id)

                if not materials:
                    material_container = xml_material_profile(container_id)
                    material_container.deserialize(archive.open(material_container_file).read().decode("utf-8"),
                                                   file_name = material_container_file)
                    containers_to_add.append(material_container)
                else:
                    material_container = materials[0]
                    if not self._container_registry.isReadOnly(container_id):  # Only create new materials if they are not read only.
                        if self._resolve_strategies["material"] == "override":
                            material_container.deserialize(archive.open(material_container_file).read().decode("utf-8"),
                                                           file_name = material_container_file)
                        elif self._resolve_strategies["material"] == "new":
                            # Note that we *must* deserialize it with a new ID, as multiple containers will be
                            # auto created & added.
                            material_container = xml_material_profile(self.getNewId(container_id))
                            material_container.deserialize(archive.open(material_container_file).read().decode("utf-8"),
                                                           file_name = material_container_file)
                            containers_to_add.append(material_container)

                material_containers.append(material_container)
                Job.yieldThread()

        Logger.log("d", "Workspace loading is checking instance containers...")
        # Get quality_changes and user profiles saved in the workspace
        instance_container_files = [name for name in cura_file_names if name.endswith(self._instance_container_suffix)]
        user_instance_containers = []
        quality_and_definition_changes_instance_containers = []
        for instance_container_file in instance_container_files:
            container_id = self._stripFileToId(instance_container_file)
            serialized = archive.open(instance_container_file).read().decode("utf-8")

            # HACK! we ignore "quality" and "variant" instance containers!
            parser = configparser.ConfigParser()
            parser.read_string(serialized)
            if not parser.has_option("metadata", "type"):
                Logger.log("w", "Cannot find metadata/type in %s, ignoring it", instance_container_file)
                continue
            if parser.get("metadata", "type") in self._ignored_instance_container_types:
                continue

            instance_container = InstanceContainer(container_id)

            # Deserialize InstanceContainer by converting read data from bytes to string
            instance_container.deserialize(serialized, file_name = instance_container_file)
            container_type = instance_container.getMetaDataEntry("type")
            Job.yieldThread()

            #
            # IMPORTANT:
            # If an instance container (or maybe other type of container) exists, and user chooses "Create New",
            # we need to rename this container and all references to it, and changing those references are VERY
            # HARD.
            #
            if container_type in self._ignored_instance_container_types:
                # Ignore certain instance container types
                Logger.log("w", "Ignoring instance container [%s] with type [%s]", container_id, container_type)
                continue
            elif container_type == "user":
                # Check if quality changes already exists.
                user_containers = self._container_registry.findInstanceContainers(id = container_id)
                if not user_containers:
                    containers_to_add.append(instance_container)
                else:
                    if self._resolve_strategies["machine"] == "override" or self._resolve_strategies["machine"] is None:
                        instance_container = user_containers[0]
                        instance_container.deserialize(archive.open(instance_container_file).read().decode("utf-8"),
                                                       file_name = instance_container_file)
                        instance_container.setDirty(True)
                    elif self._resolve_strategies["machine"] == "new":
                        # The machine is going to get a spiffy new name, so ensure that the id's of user settings match.
                        old_extruder_id = instance_container.getMetaDataEntry("extruder", None)
                        if old_extruder_id:
                            new_extruder_id = extruder_stack_id_map[old_extruder_id]
                            new_id = new_extruder_id + "_current_settings"
                            instance_container.setMetaDataEntry("id", new_id)
                            instance_container.setName(new_id)
                            instance_container.setMetaDataEntry("extruder", new_extruder_id)
                            containers_to_add.append(instance_container)

                        machine_id = instance_container.getMetaDataEntry("machine", None)
                        if machine_id:
                            new_machine_id = self.getNewId(machine_id)
                            new_id = new_machine_id + "_current_settings"
                            instance_container.setMetadataEntry("id", new_id)
                            instance_container.setName(new_id)
                            instance_container.setMetaDataEntry("machine", new_machine_id)
                            containers_to_add.append(instance_container)
                user_instance_containers.append(instance_container)
            elif container_type in ("quality_changes", "definition_changes"):
                # Check if quality changes already exists.
                changes_containers = self._container_registry.findInstanceContainers(id = container_id)
                if not changes_containers:
                    # no existing containers with the same ID, so we can safely add the new one
                    containers_to_add.append(instance_container)
                else:
                    # we have found existing container with the same ID, so we need to resolve according to the
                    # selected strategy.
                    if self._resolve_strategies[container_type] == "override":
                        instance_container = changes_containers[0]
                        instance_container.deserialize(archive.open(instance_container_file).read().decode("utf-8"),
                                                       file_name = instance_container_file)
                        instance_container.setDirty(True)

                    elif self._resolve_strategies[container_type] == "new":
                        # TODO: how should we handle the case "new" for quality_changes and definition_changes?

                        instance_container.setName(self._container_registry.uniqueName(instance_container.getName()))
                        new_changes_container_id = self.getNewId(instance_container.getId())
                        instance_container._id = new_changes_container_id

                        # TODO: we don't know the following is correct or not, need to verify
                        #       AND REFACTOR!!!
                        if self._resolve_strategies["machine"] == "new":
                            # The machine is going to get a spiffy new name, so ensure that the id's of user settings match.
                            old_extruder_id = instance_container.getMetaDataEntry("extruder", None)
                            # Note that in case of a quality_changes extruder means the definition id of the extruder stack
                            # For the user settings, it means the actual extruder stack id it's assigned to.
                            if old_extruder_id and old_extruder_id in extruder_stack_id_map:
                                new_extruder_id = extruder_stack_id_map[old_extruder_id]
                                instance_container.setMetaDataEntry("extruder", new_extruder_id)

                            machine_id = instance_container.getMetaDataEntry("machine", None)
                            if machine_id:
                                new_machine_id = self.getNewId(machine_id)
                                instance_container.setMetaDataEntry("machine", new_machine_id)

                        containers_to_add.append(instance_container)

                    elif self._resolve_strategies[container_type] is None:
                        # The ID already exists, but nothing in the values changed, so do nothing.
                        pass
                quality_and_definition_changes_instance_containers.append(instance_container)

                if container_type == "definition_changes":
                    definition_changes_extruder_count = instance_container.getProperty("machine_extruder_count", "value")
                    if definition_changes_extruder_count is not None:
                        machine_extruder_count = definition_changes_extruder_count

            else:
                existing_container = self._container_registry.findInstanceContainersMetadata(id = container_id)
                if not existing_container:
                    containers_to_add.append(instance_container)
            if global_stack_need_rename:
                if instance_container.getMetaDataEntry("machine"):
                    instance_container.setMetaDataEntry("machine", global_stack_id_new)

        # Add all the containers right before we try to add / serialize the stack
        for container in containers_to_add:
            self._container_registry.addContainer(container)
            container.setDirty(True)
            containers_added.append(container)

        # Get the stack(s) saved in the workspace.
        Logger.log("d", "Workspace loading is checking stacks containers...")

        # load global stack file
        try:
            stack = None

            if self._resolve_strategies["machine"] == "override":
                container_stacks = self._container_registry.findContainerStacks(id = global_stack_id_original)
                stack = container_stacks[0]

                # HACK
                # There is a machine, check if it has authentication data. If so, keep that data.
                network_authentication_id = stack.getMetaDataEntry("network_authentication_id")
                network_authentication_key = stack.getMetaDataEntry("network_authentication_key")
                stack.deserialize(archive.open(global_stack_file).read().decode("utf-8"), file_name = global_stack_file)
                if network_authentication_id:
                    stack.addMetaDataEntry("network_authentication_id", network_authentication_id)
                if network_authentication_key:
                    stack.addMetaDataEntry("network_authentication_key", network_authentication_key)

            elif self._resolve_strategies["machine"] == "new":
                # create a new global stack
                stack = GlobalStack(global_stack_id_new)
                # Deserialize stack by converting read data from bytes to string
                stack.deserialize(archive.open(global_stack_file).read().decode("utf-8"),
                                  file_name = global_stack_file)

                # Ensure a unique ID and name
                stack._id = global_stack_id_new

                # Extruder stacks are "bound" to a machine. If we add the machine as a new one, the id of the
                # bound machine also needs to change.
                if stack.getMetaDataEntry("machine", None):
                    stack.setMetaDataEntry("machine", global_stack_id_new)

                # Only machines need a new name, stacks may be non-unique
                stack.setName(global_stack_name_new)

                container_stacks_added.append(stack)
                self._container_registry.addContainer(stack)
                containers_added.append(stack)
            else:
                Logger.log("e", "Resolve strategy of %s for machine is not supported", self._resolve_strategies["machine"])

            # Create a new definition_changes container if it was empty
            if stack.definitionChanges == self._container_registry.getEmptyInstanceContainer():
                stack.setDefinitionChanges(CuraStackBuilder.createDefinitionChangesContainer(stack, stack.getId() + "_settings"))
            global_stack = stack
            Job.yieldThread()
        except:
            Logger.logException("w", "We failed to serialize the stack. Trying to clean up.")
            # Something went really wrong. Try to remove any data that we added.
            for container in containers_added:
                self._container_registry.removeContainer(container.getId())
            return

        # load extruder stack files
        try:
            for extruder_stack_file in extruder_stack_files:
                container_id = self._stripFileToId(extruder_stack_file)
                extruder_file_content = archive.open(extruder_stack_file, "r").read().decode("utf-8")

                if self._resolve_strategies["machine"] == "override":
                    # deserialize new extruder stack over the current ones (if any)
                    stack = self._overrideExtruderStack(global_stack, extruder_file_content, extruder_stack_file)
                    if stack is None:
                        continue

                elif self._resolve_strategies["machine"] == "new":
                    new_id = extruder_stack_id_map[container_id]
                    stack = ExtruderStack(new_id)

                    # HACK: the global stack can have a new name, so we need to make sure that this extruder stack
                    #       references to the new name instead of the old one. Normally, this can be done after
                    #       deserialize() by setting the metadata, but in the case of ExtruderStack, deserialize()
                    #       also does addExtruder() to its machine stack, so we have to make sure that it's pointing
                    #       to the right machine BEFORE deserialization.
                    extruder_config = configparser.ConfigParser()
                    extruder_config.read_string(extruder_file_content)
                    extruder_config.set("metadata", "machine", global_stack_id_new)
                    tmp_string_io = io.StringIO()
                    extruder_config.write(tmp_string_io)
                    extruder_file_content = tmp_string_io.getvalue()

                    stack.deserialize(extruder_file_content, file_name = extruder_stack_file)

                    # Ensure a unique ID and name
                    stack._id = new_id

                    self._container_registry.addContainer(stack)
                    extruder_stacks_added.append(stack)
                    containers_added.append(stack)
                else:
                    Logger.log("w", "Unknown resolve strategy: %s", self._resolve_strategies["machine"])

                # Create a new definition_changes container if it was empty
                if stack.definitionChanges == self._container_registry.getEmptyInstanceContainer():
                    stack.setDefinitionChanges(CuraStackBuilder.createDefinitionChangesContainer(stack, stack.getId() + "_settings"))

                if stack.getMetaDataEntry("type") == "extruder_train":
                    extruder_stacks.append(stack)

            # If not extruder stacks were saved in the project file (pre 3.1) create one manually
            # We re-use the container registry's addExtruderStackForSingleExtrusionMachine method for this
            if not extruder_stacks:
                stack = self._container_registry.addExtruderStackForSingleExtrusionMachine(global_stack, "fdmextruder")
                if stack:
                    if self._resolve_strategies["machine"] == "override":
                        # in case the extruder is newly created (for a single-extrusion machine), we need to override
                        # the existing extruder stack.
                        existing_extruder_stack = global_stack.extruders[stack.getMetaDataEntry("position")]
                        for idx in range(len(_ContainerIndexes.IndexTypeMap)):
                            existing_extruder_stack.replaceContainer(idx, stack._containers[idx], postpone_emit = True)
                        extruder_stacks.append(existing_extruder_stack)
                    else:
                        extruder_stacks.append(stack)

        except:
            Logger.logException("w", "We failed to serialize the stack. Trying to clean up.")
            # Something went really wrong. Try to remove any data that we added.
            for container in containers_added:
                self._container_registry.removeContainer(container.getId())
            return


        # Check quality profiles to make sure that if one stack has the "not supported" quality profile,
        # all others should have the same.
        #
        # This block code tries to fix the following problems in Cura 3.0 and earlier:
        #  1. The upgrade script can rename all "Not Supported" quality profiles to "empty_quality", but it cannot fix
        #     the problem that the global stack the extruder stacks may have different quality profiles. The code
        #     below loops over all stacks and make sure that if there is one stack with "Not Supported" profile, the
        #     rest should also use the "Not Supported" profile.
        #  2. In earlier versions (at least 2.7 and 3.0), a wrong quality profile could be assigned to a stack. For
        #     example, a UM3 can have a BB 0.8 variant with "aa04_pla_fast" quality profile enabled. To fix this,
        #     in the code below we also check the actual available quality profiles for the machine.
        #
        has_not_supported = False
        for stack in [global_stack] + extruder_stacks:
            if stack.quality.getId() in ("empty", "empty_quality"):
                has_not_supported = True
                break

        # We filter out extruder stacks that are not actually used, for example the UM3 and custom FDM printer extruder count setting.
        extruder_stacks_in_use = extruder_stacks
        if machine_extruder_count is not None:
            extruder_stacks_in_use = extruder_stacks[:machine_extruder_count]

        available_quality = QualityManager.getInstance().findAllUsableQualitiesForMachineAndExtruders(global_stack,
                                                                                                      extruder_stacks_in_use)
        if not has_not_supported:
            has_not_supported = not available_quality

        quality_has_been_changed = False

        if has_not_supported:
            empty_quality_container = self._container_registry.findInstanceContainers(id = "empty_quality")[0]
            for stack in [global_stack] + extruder_stacks_in_use:
                stack.replaceContainer(_ContainerIndexes.Quality, empty_quality_container)
            empty_quality_changes_container = self._container_registry.findInstanceContainers(id = "empty_quality_changes")[0]
            for stack in [global_stack] + extruder_stacks_in_use:
                stack.replaceContainer(_ContainerIndexes.QualityChanges, empty_quality_changes_container)
            quality_has_been_changed = True

        else:
            empty_quality_changes_container = self._container_registry.findInstanceContainers(id="empty_quality_changes")[0]

            # The machine in the project has non-empty quality and there are usable qualities for this machine.
            # We need to check if the current quality_type is still usable for this machine, if not, then the quality
            # will be reset to the "preferred quality" if present, otherwise "normal".
            available_quality_types = [q.getMetaDataEntry("quality_type") for q in available_quality]

            if global_stack.quality.getMetaDataEntry("quality_type") not in available_quality_types:
                # We are here because the quality_type specified in the project is not supported any more,
                # so we need to switch it to the "preferred quality" if present, otherwise "normal".
                quality_has_been_changed = True

                # find the preferred quality
                preferred_quality_id = global_stack.getMetaDataEntry("preferred_quality", None)
                if preferred_quality_id is not None:
                    definition_id = global_stack.definition.getId()
                    definition_id = global_stack.definition.getMetaDataEntry("quality_definition", definition_id)
                    if not parseBool(global_stack.getMetaDataEntry("has_machine_quality", "False")):
                        definition_id = "fdmprinter"

                    containers = self._container_registry.findInstanceContainers(id = preferred_quality_id,
                                                                                 type = "quality",
                                                                                 definition = definition_id)
                    containers = [c for c in containers if not c.getMetaDataEntry("material", "")]
                    if containers:
                        global_stack.quality = containers[0]
                        global_stack.qualityChanges = empty_quality_changes_container
                        # also find the quality containers for the extruders
                        for extruder_stack in extruder_stacks_in_use:
                            search_criteria = {"id": preferred_quality_id,
                                               "type": "quality",
                                               "definition": definition_id}
                            if global_stack.getMetaDataEntry("has_machine_materials") and extruder_stack.material.getId() not in ("empty", "empty_material"):
                                search_criteria["material"] = extruder_stack.material.getId()
                            containers = self._container_registry.findInstanceContainers(**search_criteria)
                            if containers:
                                extruder_stack.quality = containers[0]
                                extruder_stack.qualityChanges = empty_quality_changes_container
                            else:
                                Logger.log("e", "Cannot find preferred quality for extruder [%s].", extruder_stack.getId())

                    else:
                        # we cannot find the preferred quality. THIS SHOULD NOT HAPPEN
                        Logger.log("e", "Cannot find the preferred quality for machine [%s]", global_stack.getId())
            else:
                # The quality_type specified in the project file is usable, but the quality container itself may not
                # be correct. For example, for UM2, the quality container can be "draft" while it should be "um2_draft"
                # instead.
                # In this code branch, we try to fix those incorrect quality containers.
                #
                # ***IMPORTANT***: We only do this fix for single-extrusion machines.
                #                  We will first find the correct quality profile for the extruder, then apply the same
                #                  quality profile for the global stack.
                #
                if len(extruder_stacks) == 1:
                    extruder_stack = extruder_stacks[0]

                    search_criteria = {"type": "quality", "quality_type": global_stack.quality.getMetaDataEntry("quality_type")}
                    search_criteria["definition"] = global_stack.definition.getId()
                    if not parseBool(global_stack.getMetaDataEntry("has_machine_quality", "False")):
                        search_criteria["definition"] = "fdmprinter"

                    if global_stack.getMetaDataEntry("has_machine_materials") and extruder_stack.material.getId() not in ("empty", "empty_material"):
                        search_criteria["material"] = extruder_stack.material.getId()
                    containers = self._container_registry.findInstanceContainers(**search_criteria)
                    if containers:
                        new_quality_container = containers[0]
                        extruder_stack.quality = new_quality_container
                        global_stack.quality = new_quality_container

        # Replacing the old containers if resolve is "new".
        # When resolve is "new", some containers will get renamed, so all the other containers that reference to those
        # MUST get updated too.
        #
        if self._resolve_strategies["machine"] == "new":
            # A new machine was made, but it was serialized with the wrong user container. Fix that now.
            for container in user_instance_containers:
                # replacing the container ID for user instance containers for the extruders
                extruder_id = container.getMetaDataEntry("extruder", None)
                if extruder_id:
                    for extruder in extruder_stacks:
                        if extruder.getId() == extruder_id:
                            extruder.userChanges = container
                            continue

                # replacing the container ID for user instance containers for the machine
                machine_id = container.getMetaDataEntry("machine", None)
                if machine_id:
                    if global_stack.getId() == machine_id:
                        global_stack.userChanges = container
                        continue

        changes_container_types = ("quality_changes", "definition_changes")
        if quality_has_been_changed:
            # DO NOT replace quality_changes if the current quality_type is not supported
            changes_container_types = ("definition_changes",)
        for changes_container_type in changes_container_types:
            if self._resolve_strategies[changes_container_type] == "new":
                # Quality changes needs to get a new ID, added to registry and to the right stacks
                for each_changes_container in quality_and_definition_changes_instance_containers:
                    # NOTE: The renaming and giving new IDs are possibly redundant because they are done in the
                    #       instance container loading part.
                    new_id = each_changes_container.getId()

                    # Find the old (current) changes container in the global stack
                    if changes_container_type == "quality_changes":
                        old_container = global_stack.qualityChanges
                    elif changes_container_type == "definition_changes":
                        old_container = global_stack.definitionChanges

                    # sanity checks
                    # NOTE: The following cases SHOULD NOT happen!!!!
                    if not old_container:
                        Logger.log("e", "We try to get [%s] from the global stack [%s] but we got None instead!",
                                   changes_container_type, global_stack.getId())

                    # Replace the quality/definition changes container if it's in the GlobalStack
                    # NOTE: we can get an empty container here, but the IDs will not match,
                    # so this comparison is fine.
                    if self._id_mapping.get(old_container.getId()) == new_id:
                        if changes_container_type == "quality_changes":
                            global_stack.qualityChanges = each_changes_container
                        elif changes_container_type == "definition_changes":
                            global_stack.definitionChanges = each_changes_container
                        continue

                    # Replace the quality/definition changes container if it's in one of the ExtruderStacks
                    for each_extruder_stack in extruder_stacks:
                        changes_container = None
                        if changes_container_type == "quality_changes":
                            changes_container = each_extruder_stack.qualityChanges
                        elif changes_container_type == "definition_changes":
                            changes_container = each_extruder_stack.definitionChanges

                        # sanity checks
                        # NOTE: The following cases SHOULD NOT happen!!!!
                        if not changes_container:
                            Logger.log("e", "We try to get [%s] from the extruder stack [%s] but we got None instead!",
                                       changes_container_type, each_extruder_stack.getId())

                        # NOTE: we can get an empty container here, but the IDs will not match,
                        # so this comparison is fine.
                        if self._id_mapping.get(changes_container.getId()) == new_id:
                            if changes_container_type == "quality_changes":
                                each_extruder_stack.qualityChanges = each_changes_container
                            elif changes_container_type == "definition_changes":
                                each_extruder_stack.definitionChanges = each_changes_container

        if self._resolve_strategies["material"] == "new":
            # the actual material instance container can have an ID such as
            #  <material>_<machine>_<variant>
            # which cannot be determined immediately, so here we use a HACK to find the right new material
            # instance ID:
            #  - get the old material IDs for all material
            #  - find the old material with the longest common prefix in ID, that's the old material
            #  - update the name by replacing the old prefix with the new
            #  - find the new material container and set it to the stack
            old_to_new_material_dict = {}
            for each_material in material_containers:
                # find the material's old name
                for old_id, new_id in self._id_mapping.items():
                    if each_material.getId() == new_id:
                        old_to_new_material_dict[old_id] = each_material
                        break

            # replace old material in global and extruder stacks with new
            self._replaceStackMaterialWithNew(global_stack, old_to_new_material_dict)
            if extruder_stacks:
                for each_extruder_stack in extruder_stacks:
                    self._replaceStackMaterialWithNew(each_extruder_stack, old_to_new_material_dict)

        if extruder_stacks:
            for stack in extruder_stacks:
                ExtruderManager.getInstance().registerExtruder(stack, global_stack.getId())

        Logger.log("d", "Workspace loading is notifying rest of the code of changes...")

        if self._resolve_strategies["machine"] == "new":
            for stack in extruder_stacks:
                stack.setNextStack(global_stack)
                stack.containersChanged.emit(stack.getTop())

        # Actually change the active machine.
        Application.getInstance().setGlobalContainerStack(global_stack)

        # Notify everything/one that is to notify about changes.
        global_stack.containersChanged.emit(global_stack.getTop())

        # Load all the nodes / meshdata of the workspace
        nodes = self._3mf_mesh_reader.read(file_name)
        if nodes is None:
            nodes = []

        base_file_name = os.path.basename(file_name)
        if base_file_name.endswith(".curaproject.3mf"):
            base_file_name = base_file_name[:base_file_name.rfind(".curaproject.3mf")]
        self.setWorkspaceName(base_file_name)
        return nodes

    ##  HACK: Replaces the material container in the given stack with a newly created material container.
    #         This function is used when the user chooses to resolve material conflicts by creating new ones.
    def _replaceStackMaterialWithNew(self, stack, old_new_material_dict):
        # The material containers in the project file are 'parent' material such as "generic_pla",
        # but a material container used in a global/extruder stack is a 'child' material,
        # such as "generic_pla_ultimaker3_AA_0.4", which can be formalised as the following:
        #
        #    <material_name>_<machine_name>_<variant_name>
        #
        # In the project loading, when a user chooses to resolve material conflicts by creating new ones,
        # the old 'parent' material ID and the new 'parent' material ID are known, but not the child material IDs.
        # In this case, the global stack and the extruder stacks need to use the newly created material, but the
        # material containers they use are 'child' material. So, here, we need to find the right 'child' material for
        # the stacks.
        #
        # This hack approach works as follows:
        #   - No matter there is a child material or not, the actual material we are looking for has the prefix
        #     "<material_name>", which is the old material name. For the material in a stack, we know that the new
        #     material's ID will be "<new_material_name>_blabla..", so we just need to replace the old material ID
        #     with the new one to get the new 'child' material.
        #   - Because the material containers have IDs such as "m #nn", if we use simple prefix matching, there can
        #     be a problem in the following scenario:
        #        - there are two materials in the project file, namely "m #1" and "m #11"
        #        - the child materials in use are for example: "m #1_um3_aa04", "m #11_um3_aa04"
        #        - if we only check for a simple prefix match, then "m #11_um3_aa04" will match with "m #1", but they
        #          are not the same material
        #     To avoid this, when doing the prefix matching, we use the result with the longest mactching prefix.

        # find the old material ID
        old_material_id_in_stack = stack.material.getId()
        best_matching_old_material_id = None
        best_matching_old_meterial_prefix_length = -1
        for old_parent_material_id in old_new_material_dict:
            if len(old_parent_material_id) < best_matching_old_meterial_prefix_length:
                continue
            if len(old_parent_material_id) <= len(old_material_id_in_stack):
                if old_parent_material_id == old_material_id_in_stack[0:len(old_parent_material_id)]:
                    best_matching_old_meterial_prefix_length = len(old_parent_material_id)
                    best_matching_old_material_id = old_parent_material_id

        if best_matching_old_material_id is None:
            Logger.log("w", "Cannot find any matching old material ID for stack [%s] material [%s]. Something can go wrong",
                       stack.getId(), old_material_id_in_stack)
            return

        # find the new material container
        new_material_id = old_new_material_dict[best_matching_old_material_id].getId() + old_material_id_in_stack[len(best_matching_old_material_id):]
        new_material_containers = self._container_registry.findInstanceContainers(id = new_material_id, type = "material")
        if not new_material_containers:
            Logger.log("e", "Cannot find new material container [%s]", new_material_id)
            return

        # replace the material in the given stack
        stack.material = new_material_containers[0]

    def _stripFileToId(self, file):
        mime_type = MimeTypeDatabase.getMimeTypeForFile(file)
        file = mime_type.stripExtension(file)
        return file.replace("Cura/", "")

    def _getXmlProfileClass(self):
        return self._container_registry.getContainerForMimeType(MimeTypeDatabase.getMimeType("application/x-ultimaker-material-profile"))

    ##  Get the list of ID's of all containers in a container stack by partially parsing it's serialized data.
    def _getContainerIdListFromSerialized(self, serialized):
        parser = configparser.ConfigParser(interpolation=None, empty_lines_in_values=False)
        parser.read_string(serialized)

        container_ids = []
        if "containers" in parser:
            for index, container_id in parser.items("containers"):
                container_ids.append(container_id)
        elif parser.has_option("general", "containers"):
            container_string = parser["general"].get("containers", "")
            container_list = container_string.split(",")
            container_ids = [container_id for container_id in container_list if container_id != ""]

        # HACK: there used to be 6 containers numbering from 0 to 5 in a stack,
        #       now we have 7: index 5 becomes "definition_changes"
        if len(container_ids) == 6:
            # Hack; We used to not save the definition changes. Fix this.
            container_ids.insert(5, "empty")

        return container_ids

    def _getMachineNameFromSerializedStack(self, serialized):
        parser = configparser.ConfigParser(interpolation=None, empty_lines_in_values=False)
        parser.read_string(serialized)
        return parser["general"].get("name", "")

    def _getMaterialLabelFromSerialized(self, serialized):
        data = ET.fromstring(serialized)
        metadata = data.iterfind("./um:metadata/um:name/um:label", {"um": "http://www.ultimaker.com/material"})
        for entry in metadata:
            return entry.text
