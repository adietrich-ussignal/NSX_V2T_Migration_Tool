# ***************************************************
# Copyright © 2021 VMware, Inc. All rights reserved.
# ***************************************************

"""
Description: Module which performs all the validations for v2tAssessment before migrating the VMware Cloud Director from NSX-V to NSX-T
"""

import copy
import csv
import datetime
import getpass
import logging
import math
import os
import prettytable
import sys
import traceback
from collections import OrderedDict
from src import constants
from src.commonUtils import utils
from src.core.vcd import vcdConstants
from src.core.vcd.vcdValidations import VCDMigrationValidation
from src.rollback import Rollback

# Set path till src folder in PYTHONPATH
cwd = os.getcwd()
parentDir = os.path.abspath(os.path.join(cwd, os.pardir))
sys.path.append(parentDir)
from src.commonUtils.threadUtils import Thread, waitForThreadToComplete

# Status codes are assigned to each orgVDC after completion of its assessment
# e. g.: if any single validations from 'Blocking' category failed, status will be
# assigned as 'Cannot be migrated'(2)
STATUS_CODES = OrderedDict({
    0: 'Can be migrated',
    1: 'Can be migrated with additional preparation work',
    2: 'Automated migration not supported with the current version',
    3: 'Org VDC not accessible for assessment',
    4: 'Org VDC not present'
})

# VALIDATION_CODES are used to classify features mentioned in VALIDATION_CLASSIFICATION.
# If any feature is not mentioned in VALIDATION_CLASSIFICATION, status code from
# NOT_CATEGORIZED_VALIDATION_CODE will be applicable
NOT_CATEGORIZED_VALIDATION_CODE = 99
VALIDATION_CODES = {
    1: 'Can be mitigated',
    2: 'Blocking',
    NOT_CATEGORIZED_VALIDATION_CODE: 'Blocking',
}

# Each validation is assigned a code from VALIDATION_CODES based upon its mitigation effort
VALIDATION_CLASSIFICATION = {
    'Empty vApps': 1,
    'Suspended VMs': 1,
    'Routed vApp Networks': 2,
    'Fencing enabled on vApps': 2,
    'No free interface on edge gateways': 1,
    'Edge Gateway Rate Limit': 1,
    'Independent Disks: Shared disk present': 2,
    'Independent Disks: Attached VMs are not powered off': 1,
    'DHCP Binding: Binding IP addresses overlaps with static IP Pool range': 1,
    'DHCP Relay: Domain names are configured': 1,
    'DHCP Relay: IP sets are configured': 1,
    'Gateway Firewall: Any as TCP/UDP port': 1,
    'Gateway Firewall: Gateway Interfaces in rule': 1,
    'Gateway Firewall: Networks connected to different edge gateway used': 1,
    'Gateway Firewall: Unsupported grouping object': 1,
    'NAT: NAT64 rule': 2,
    'NAT: Range of IPs or network in DNAT rule': 1,
    'IPsec: Route based session type': 2,
    'IPsec: Unsupported Encryption Algorithm': 1,
    'IPsec: CA certificate is missing': 1,
    'IPsec: DNAT rules not supported with Policy-based session type': 2,
    'OSPF routing protocol': 2,
    'User-defined Static Routes': 1,
    'LoadBalancer: Transparent Mode': 2,
    'LoadBalancer: Application Rules': 2,
    'LoadBalancer: Default pool not configured': 1,
    'LoadBalancer: Unsupported persistence': 1,
    'LoadBalancer: Unsupported algorithm': 1,
    'L2VPN service': 2,
    'SSLVPN service': 2,
    'Distributed Firewall: Invalid objects in rule': 1,
    'Distributed Firewall: Unsupported type in applied to section': 1,
    'Distributed Firewall: Networks connected to different edge gateway used': 1,
    'Distributed Firewall: Layer 2 Rule': 1,
    'Distributed Firewall: Invalid Security Group objects in rule': 1,
    'Syslog service': 1,
    'SSH service': 1,
    'Cross VDC Networking': 2,
    'GRE Tunnel': 2,
}


class VMwareCloudDirectorNSXMigratorV2T:
    """
    Description :   The class has methods which does v2t-Assessment tasks from NSX-V to NSX-T
    """
    def __init__(self, inputDict, buildVersion=None):
        """
        Description : This method initializes the basic configurations reqired to run Assessment mode
        Parameter: inputDict - dictionary that holds all the input file values (DICT)
                   vCloudDirectorPassword - password of VMware vCloud Director (STRING)
        """
        self.consoleLogger = logging.getLogger("consoleLogger")

        self.logger = logging.getLogger("mainLogger")

        # Build version
        self.buildVersion = buildVersion

        v2tAssessmentLogFileName = logging.getLogger("consoleLogger").handlers[1].baseFilename
        self.inputDict = inputDict

        # Validating the input file
        self.inputValidation()

        # creating object of rollback class
        self.rollback = Rollback(self.consoleLogger)

        # List the holds the data of the v2tAssessment
        self.reportData = []
        self.summaryColumnLength = None
        self.reportBasePath = os.path.join(constants.parentRootDir, "reports")

        # logging the certificate validation warning in case of verify set to False
        if not self.inputDict['VCloudDirector']['verify']:
            warningMessage = '\n' + '*' * 100 + '\n*' + ('Certificate validation disabled for - VMware vCloud Director'.center(98) + '*\n' + '*' * 100)
            self.consoleLogger.warning(warningMessage)

        # Validating the certificate file and adding SSL certificate if verify is set to True
        if self.inputDict['VCloudDirector']['verify']:
            # update certificate path in requests
            certPath = self.inputDict.get('Common', {}).get('CertificatePath', None)
            utils.Utilities().updateRequestsPemCert(certPath)

        # Getting password of VMware vCloud Director
        vCloudDirectorPassword = self._getVcloudDirectorPassword()

        threadObj = Thread(maxNumberOfThreads=self.threadCount)

        # Creating object of vcd validation class
        self.vcdValidationObj = self.vcdValidationObj = VCDMigrationValidation(
            self.inputDict['VCloudDirector']['ipAddress'],
            self.inputDict['VCloudDirector']['username'],
            vCloudDirectorPassword,
            self.inputDict['VCloudDirector']['verify'],
            self.rollback, threadObj, vdcName="MainThread")

        # Login to vCD
        self.vcdValidationObj.vcdLogin()
        self.consoleLogger.info('Logged in to VMware Cloud Director {}'.format(self.inputDict['VCloudDirector']['ipAddress']))

        self.vcdValidationMapping = dict()

        # Fetching the datetime from the log file
        self.currentDateTime = os.path.basename(v2tAssessmentLogFileName).replace('VCD-NSX-Migrator-v2tAssessment-Log-', '').replace('.log', '')

        # Setting the version of vCD
        self.version = self.vcdValidationObj.version

        # Starting time of assessment
        self.initialTime = datetime.datetime.now()

        # Create reports directory if it is not present
        if not os.path.exists(self.reportBasePath):
            os.mkdir(self.reportBasePath)

    def _getVcloudDirectorPassword(self):
        """
        Description :   getting VMware Cloud Director password from user
        """
        vCloudDirectorPassword = getpass.getpass(prompt="Please enter VMware Cloud Director Password: ")
        if not vCloudDirectorPassword:
            raise ValueError("VMware Cloud Director password must be provided")
        return vCloudDirectorPassword

    def inputValidation(self):
        """
            Description: This method validates the input file for v2tAssessment mode
        """
        # Error list for input validations
        errorList = list()
        if self.inputDict.get("VCloudDirector", {}) == None:
            self.inputDict["VCloudDirector"] = {}
        if not self.inputDict.get("VCloudDirector", {}).get("ipAddress", None):
            errorList.append("VCloudDirector '[ipAddress]' must be provided")
        if not self.inputDict.get("VCloudDirector", {}).get("username", None):
            errorList.append("VCloudDirector '[username]' must be provided")
        if self.inputDict.get("VCloudDirector", {}).get("verify", None) == None:
            errorList.append("VCloudDirector '[verify]' must be provided")
        if self.inputDict.get("VCloudDirector", {}).get("verify", None) \
                and not isinstance(self.inputDict.get("VCloudDirector", {}).get("verify", None), bool):
            errorList.append("VCloudDirector '[verify]' Value must be boolean i.e either True or False.")
        if self.inputDict.get("Organization", None) and not isinstance(self.inputDict.get("Organization"), list):
            errorList.append("'Organization' Value must be a List")
        if self.inputDict.get("OrgVDC", None) and not isinstance(self.inputDict.get("OrgVDC"), list):
            errorList.append("'OrgVDC' Value must be a List")
        if (self.inputDict.get('VCloudDirector') or {}).get('verify'):
            certPath = (self.inputDict.get('Common') or {}).get('CertificatePath')
            if not certPath:
                errorList.append("Verify is set to 'True' but certificate path is not provided in user Input file")
            # checking for the path provided in user input whether its valid
            elif not os.path.exists(certPath):
                errorList.append(f"The provided certificate path '{certPath}' in user Input file does not exist.")

        try:
            self.threadCount = int((self.inputDict.get("Common") or {}).get("MaxThreadCount") or 75)
        except (ValueError, AttributeError, TypeError):
            errorList.append("Common '[MaxThreadCount]', Value must be integer")

        if errorList:
            raise Exception('Input Validation Error - {}'.format('\n'.join(errorList)))

    def checkOrgVDCDetails(self, orgName, vdcName):
        """
            Description : This method fetches the details of OrgUrl and OrgVDCDetails
            Parameter: orgName - Name of the organization (STRING)
                       vdcName - Name of the org vdc (STRING)
        """
        try:
            self.consoleLogger.debug('Getting Org VDC {} details'.format(vdcName))
            orgUrl = self.vcdValidationObj.getOrgUrl(orgName)
            sourceOrgVDCId = self.vcdValidationObj.getOrgVDCDetails(orgUrl, vdcName, 'sourceOrgVDC')
            backingType = self.vcdValidationObj.getBackingTypeOfOrgVDC(sourceOrgVDCId)
            if backingType != "NSX_V":
                return Exception(f'{vdcName} is not NSX-V backed')
        except Exception:
            raise

    def initializeV2TValidations(self, orgName, OrgId, vdcName, vdcId):
        """
            Description : This method fetches the necessary details to run validations
        """
        try:
            # Fetching and saving data of orgVDC to be validated in apiData
            self.checkOrgVDCDetails(orgName, vdcName=vdcName)

            getEdgeGatewayDesc = 'Getting details of source edge gateway list'
            # fetch details of edge gateway
            self.consoleLogger.info(getEdgeGatewayDesc)
            self.edgeGatewayIdList = self.vcdValidationObj.getOrgVDCEdgeGatewayId(vdcId)
            if isinstance(self.edgeGatewayIdList, Exception):
                raise self.edgeGatewayIdList

            # Validation methods reference
            self.vcdValidationMapping = {
                'Empty vApps': [self.vcdValidationObj.validateNoEmptyVappsExistInSourceOrgVDC, vdcId],
                'Suspended VMs': [self.vcdValidationObj.validateSourceSuspendedVMsInVapp, vdcId],
                'Routed vApp Networks': [self.vcdValidationObj.validateNoVappNetworksExist, vdcId],
                'Fencing enabled on vApps': [self.vcdValidationObj.validateVappFencingMode, vdcId],
                'No free interface on edge gateways': [self.vcdValidationObj.validateEdgeGatewayUplinks,
                                                     vdcId, self.edgeGatewayIdList],
                'Edge Gateway Rate Limit': [self.vcdValidationObj.validateEdgeGatewayRateLimit, self.edgeGatewayIdList],
                'Independent Disks': [self.vcdValidationObj.validateIndependentDisks, vdcId, OrgId, True],
                'Validating Source Edge gateway services': [self.vcdValidationObj.getEdgeGatewayServices, None, None, None, True, None, True],
                'Unsupported DFW configuration': [self.vcdValidationObj.getDistributedFirewallConfig, vdcId, True, True, True],
                'Cross VDC Networking': [self.vcdValidationObj.validateCrossVdcNetworking, vdcId]
            }
        except Exception:
            raise

    def changeLoggingFormat(self, vdcName=str(), restore=False):
        """
            Description : This method changes the main logger format to specify the logs specific to org VDC
            Parameter: vdcName - Name of the org vdc (STRING)
                       restore - Flag the decides to restore logging format or not (BOOL)
        """
        # Default logging format
        defaultFormat = logging.Formatter("%(asctime)s [%(module)s]:[%(funcName)s]:%(lineno)d [%(levelname)s] | %(message)s")
        # Custom logging format
        customFormat = logging.Formatter(f"%(asctime)s [%(module)s]:[%(funcName)s]:%(lineno)d [%(levelname)s] [{vdcName}] | %(message)s")

        # Fetching the logger object for console logger
        logger = logging.getLogger("consoleLogger")
        for handler in logger.handlers:
            if handler.name == "main":
                if restore:
                    # Restoring logger format to default format
                    handler.setFormatter(defaultFormat)
                else:
                    # Changing log format to custom format
                    handler.setFormatter(customFormat)
                break
        # Fetching the logger object for main logger
        logger = logging.getLogger("mainLogger")
        for handler in logger.handlers:
            if handler.name == "main":
                if restore:
                    # Restoring logger format to default format
                    handler.setFormatter(defaultFormat)
                else:
                    # Changing log format to custom format
                    handler.setFormatter(customFormat)
                break

    def changeLogLevelForConsoleLog(self, disable=True):
        """
            Description : Disables the console logs while executing the evaluation function for a org vdc
            Parameters: disable - Flag the decides whether to enable of disable the logs (BOOLEAN)
        """
        # Fetching the logger object for console logger
        logger = logging.getLogger("consoleLogger")
        for handler in logger.handlers:
            if handler.name == "console":
                if disable:
                    # Activate console logging handler at the warning level
                    handler.setLevel(logging.ERROR)
                else:
                    # Activate console logging handler at the info level
                    handler.setLevel(logging.INFO)
                break
        # Fetching the logger object for main logger
        logger = logging.getLogger("mainLogger")
        for handler in logger.handlers:
            if handler.name == "console":
                if disable:
                    # Activate console logging handler at the warning level
                    handler.setLevel(logging.ERROR)
                else:
                    # Activate console logging handler at the info level
                    handler.setLevel(logging.INFO)
                break

    def createMapping(self):
        """
            Description : Creates a mapping of org vdc to its corresponding organization
            Return: Dictionary holding the org vdc to organization mapping along with the org vdc id (DICT)
        """
        # List to get all the error related to user input
        errors = []
        # Dict that holds relation map for org vdc to organization
        relationMap = {}
        # Fetching the details of all the Org VDC'S
        orgVDCs = self.vcdValidationObj.getAllOrgVdc()
        # Path to follow if OrgVDC key is provided in user input file
        if self.inputDict.get("OrgVDC"):
            for userDefinedVDC in self.inputDict.get("OrgVDC"):
                # Flag to check if org vdc is found or not
                matchFound = False
                for VDC in orgVDCs:
                    # Condition to check is organization along with org vdc is also provided
                    if isinstance(userDefinedVDC, dict):
                        # Fetching org vdc and org name from list
                        vdcName, orgName = list(userDefinedVDC.items())[0]
                        # if org vdc name and org name match is found go further
                        if vdcName == VDC['name'] and orgName == VDC['org']['name']:
                            try:
                            # Check if the org vdc provided is NSX-V backed
                                if isinstance(self.checkOrgVDCDetails(orgName, vdcName=vdcName), Exception):  #not NSX-V backed
                                    errors.append(f"{vdcName} is not NSX-V backed")
                                    matchFound = True
                                    break
                                if any([data['Key'].endswith("-v2t")
                                    for data in self.vcdValidationObj.getOrgVDCMetadata(orgVDCId=VDC['id'],
                                                                                       rawData=True)]):
                                    errors.append(f'Org VDC "{vdcName}" is already under migration')
                                    matchFound = True
                                    break
                            except:
                                matchFound = False
                                break
                            # Adding the org vdc and org to relation map
                            matchFound = True
                            if VDC['org']['name'] not in relationMap:
                                relationMap[VDC['org']['name']] = {}
                            relationMap[VDC['org']['name']][vdcName] = VDC['id']
                            break
                    # Condition to check if only org vdc name is provided
                    elif userDefinedVDC == VDC['name']:
                        vdcName = VDC['name']
                        orgName = VDC['org']['name']
                        try:
                            # Check if org vdc provided is NSX-V backed
                            if isinstance(self.checkOrgVDCDetails(orgName, vdcName=vdcName), Exception):
                                errors.append(f"{vdcName} is not NSX-V backed")
                                matchFound = True
                                break
                            if any([data['Key'].endswith("-v2t")
                                    for data in self.vcdValidationObj.getOrgVDCMetadata(orgVDCId=VDC['id'],
                                                                                    rawData=True)]):
                                errors.append(f'Org VDC "{vdcName}" is already under migration')
                                matchFound = True
                                break
                        except:
                            matchFound = False
                            break

                        # add the org vdc along with org in relation map
                        matchFound = True
                        if VDC['org']['name'] not in relationMap:
                            relationMap[VDC['org']['name']] = {}
                        relationMap[VDC['org']['name']][userDefinedVDC] = VDC['id']
                if not matchFound:
                    if isinstance(userDefinedVDC, dict):
                        if orgName not in relationMap:
                            relationMap[orgName] = {}
                        relationMap[orgName][vdcName] = 'NA'
                    else:
                        if 'NA' not in relationMap:
                            relationMap['NA'] = {}
                        relationMap['NA'][userDefinedVDC] = 'NA'
                    self.consoleLogger.warning(f'Org VDC "{userDefinedVDC}" does not exist')
        # Path to follow if Organization key is provided in user input file
        elif self.inputDict.get("Organization"):
            # Iterating over the list of organizations provided in the user input file
            for org in self.inputDict.get("Organization"):
                # Check if the organization exists in the vCD
                try:
                    self.vcdValidationObj.getOrgUrl(org)
                except:
                    self.consoleLogger.warning(f'Organization {org} does not exist')
                    continue

            # Iterating over org vdc to fetch nsx_v backed org vdc/s
            for VDC in orgVDCs:
                orgName = VDC['org']['name']
                vdcName = VDC['name']
                # If org name matches the one in user input go further
                if orgName in self.inputDict.get("Organization"):
                    # Check is the org vdc is NSX-V backed and migration is not under progress
                    if not isinstance(self.checkOrgVDCDetails(orgName, vdcName=vdcName), Exception) and not any(
                            [data['Key'].endswith("-v2t")
                             for data in self.vcdValidationObj.getOrgVDCMetadata(orgVDCId=VDC['id'], rawData=True)]):
                        # Adding the org vdc in the relation map
                        if orgName not in relationMap:
                            relationMap[orgName] = {}
                        relationMap[orgName][vdcName] = VDC['id']
            # If no NSX-V backed org vdc is present in the provided org/s then raise a exception
            if "does not exist" not in ''.join(errors) and not relationMap:
                errors.append(f"No NSX-V backed org VDC that is not under migration is available in Organization/s - {', '.join(self.inputDict.get('Organization'))}")
        # Path to follow when neither OrgVDC or Organization is provided in user input
        else:
            # Iterating over all the org vdc's in the vCD
            for VDC in orgVDCs:
                orgName = VDC['org']['name']
                vdcName = VDC['name']
                # Checking if the org vdc is NSX-V backed and migration is not under progress
                if not isinstance(self.checkOrgVDCDetails(orgName, vdcName=vdcName), Exception) and not any(
                            [data['Key'].endswith("-v2t")
                             for data in self.vcdValidationObj.getOrgVDCMetadata(orgVDCId=VDC['id'], rawData=True)]):
                    # Adding the org in relation map
                    if orgName not in relationMap:
                        relationMap[orgName] = {}
                    relationMap[orgName][vdcName] = VDC['id']
            if not relationMap:
                errors.append("No NSX-V backed org VDC that is not under migration is available")

        # If there are error/s, raise exception with that error/s
        if errors:
            raise Exception("Cannot continue due to the following error/s: "+"\n"+"\n".join(errors))

        return relationMap

    def run(self):
        """
        Description : Method that executes the v2t Assessment mode
        """
        try:
            # Getting vcd UUID
            self.vcdUUID = self.vcdValidationObj.getVCDuuid().split(":")[-1]

            # Creating organization and org vdc mapping
            relationMap = self.createMapping()
            # Fetching vm related data for all the NSX-V backed OrgVDC's
            vmData = self.vcdValidationObj.getVMsRelatedDataOfOrgVdc()

            # Fetching vCD version for vCD cells data
            self.vcdVersion = self.vcdValidationObj.getVCDVersion()

            # Iterating over the org in the relation map
            for org in relationMap:
                # Iterating over the org vdc's in the relation map
                for VDC, VDCId in relationMap[org].items():
                    # Change logging format
                    self.changeLoggingFormat(f"{VDC}:{org}")

                    # Reinitalizing apiData for every subsequent usage
                    self.vcdValidationObj.rollback.apiData = {}

                    # Dict to store org vdc result, populating the pre-fetched data for this org vdc
                    self.orgVDCResult = OrderedDict()
                    self.orgVDCResult['Org Name'] = org
                    self.orgVDCResult['Org VDC'] = VDC
                    orgVdcExists = True
                    try:
                        orgUrl = self.vcdValidationObj.getOrgUrl(org)
                        orgVdc = self.vcdValidationObj.getOrgVDCUrl(orgUrl, VDC, saveResponse=True)
                    except:
                        orgVdcExists = False

                    self.orgVDCResult['Org VDC UUID'] = VDCId.split(":")[-1] if orgVdcExists else 'NA'
                    self.orgVDCResult['Status'] = STATUS_CODES[0] if orgVdcExists else STATUS_CODES[4]
                    self.orgVDCResult['VMs'] = vmData[org][VDC]['numberOfVMs'] if orgVdcExists else 0
                    self.orgVDCResult['ORG VDC RAM (MB)'] = vmData[org][VDC]['memoryUsedMB'] if orgVdcExists else 0
                    self.orgVDCResult['Number of Networks to Bridge'] = 0
                    # Attribute provide count of initial columns in report which
                    # provides summary before adding actual validation features
                    self.summaryColumnCount = len(self.orgVDCResult)
                    # Adding the result before executing validation
                    for key, value in VALIDATION_CLASSIFICATION.items():
                        self.orgVDCResult[key] = "NA"

                    if not orgVdcExists:
                        self.reportData.append(self.orgVDCResult)
                        continue

                    self.consoleLogger.info(f"Evaluating Org VDC '{VDC}' of organization '{org}'")
                    # Changing log level of console logger
                    self.changeLogLevelForConsoleLog(disable=True)

                    try:
                        # getting the source Org VDC networks
                        self.consoleLogger.debug('Getting the Org VDC networks of source Org VDC {}'.format(VDC))
                        self.orgVdcNetworkList = self.vcdValidationObj.getOrgVDCNetworks(VDCId, 'sourceOrgVDCNetworks')
                        # Filtering networks to get networks that require briding
                        filteredList = copy.deepcopy(self.orgVdcNetworkList)
                        filteredList = list(filter(lambda network: network['networkType'] != 'DIRECT', filteredList))

                        # Adding number of networks to be bridged in the detailed report
                        self.orgVDCResult['Number of Networks to Bridge'] = len(filteredList)

                        # Initializing the necessities for validation of a org vdc
                        orgId = self.vcdValidationObj.getOrgId(org)
                        self.initializeV2TValidations(org, orgId, VDC, VDCId)

                        # Iterating over the validations and start executing validations one by one
                        for desc, method in self.vcdValidationMapping.items():
                            methodName = method.pop(0)
                            argsList = method
                            skipHere = False
                            for eachArg in argsList:
                                if isinstance(eachArg, Exception):
                                    skipHere = True
                                    break
                            if skipHere == True:
                                continue
                            else:
                                # Run method
                                output = self.runV2TValidations(desc, methodName, argsList)
                                # If the method is validating edgegateway services get the output and process for report
                                if desc == "Unsupported DFW configuration":
                                    del self.orgVDCResult["Unsupported DFW configuration"]
                                    dfwResult = output
                                    if "has invalid objects" in ''.join(dfwResult):
                                        self.orgVDCResult["Distributed Firewall: Invalid objects in rule"] = True
                                    else:
                                        self.orgVDCResult["Distributed Firewall: Invalid objects in rule"] = False

                                    if "has invalid security group objects" in ''.join(dfwResult) and 'Security Group' in ''.join(dfwResult):
                                        self.orgVDCResult["Distributed Firewall: Invalid Security Group objects in rule"] = True
                                    else:
                                        self.orgVDCResult["Distributed Firewall: Invalid Security Group objects in rule"] = False

                                    if "provided in applied to section in rule" in ''.join(dfwResult):
                                        self.orgVDCResult["Distributed Firewall: Unsupported type in applied to section"] = True
                                    else:
                                        self.orgVDCResult["Distributed Firewall: Unsupported type in applied to section"] = False

                                    if "are connected to different edge gateways" in ''.join(dfwResult):
                                        self.orgVDCResult["Distributed Firewall: Networks connected to different edge gateway used"] = True
                                    else:
                                        self.orgVDCResult["Distributed Firewall: Networks connected to different edge gateway used"] = False

                                    if "Layer2 rule present" in ''.join(dfwResult):
                                        self.orgVDCResult["Distributed Firewall: Layer 2 Rule"] = True
                                    else:
                                        self.orgVDCResult["Distributed Firewall: Layer 2 Rule"] = False

                                if desc == "Validating Source Edge gateway services":
                                    del self.orgVDCResult["Validating Source Edge gateway services"]
                                    servicesResult = output
                                    for serviceName, result in servicesResult.items():
                                        if serviceName == "LoadBalancer":
                                            if "transparent mode enabled" in ''.join(result):
                                                self.orgVDCResult["LoadBalancer: Transparent Mode"] = True
                                            else:
                                                self.orgVDCResult["LoadBalancer: Transparent Mode"] = False
                                            if "Application rules" in ''.join(result):
                                                self.orgVDCResult["LoadBalancer: Application Rules"] = True
                                            else:
                                                self.orgVDCResult["LoadBalancer: Application Rules"] = False
                                            if "Default pool is not configured" in ''.join(result):
                                                self.orgVDCResult["LoadBalancer: Default pool not configured"] = True
                                            else:
                                                self.orgVDCResult["LoadBalancer: Default pool not configured"] = False
                                            if "Unsupported persistence" in ''.join(result):
                                                self.orgVDCResult["LoadBalancer: Unsupported persistence"] = True
                                            else:
                                                self.orgVDCResult["LoadBalancer: Unsupported persistence"] = False
                                            if "Unsupported algorithm" in ''.join(result):
                                                self.orgVDCResult["LoadBalancer: Unsupported algorithm"] = True
                                            else:
                                                self.orgVDCResult["LoadBalancer: Unsupported algorithm"] = False
                                        if serviceName == "DHCP":
                                            if "Domain names are configured as a DHCP servers" in ''.join(result):
                                                self.orgVDCResult["DHCP Relay: Domain names are configured"] = True
                                            else:
                                                self.orgVDCResult["DHCP Relay: Domain names are configured"] = False
                                            if "IP sets are configured as a DHCP servers" in ''.join(result):
                                                self.orgVDCResult["DHCP Relay: IP sets are configured"] = True
                                            else:
                                                self.orgVDCResult["DHCP Relay: IP sets are configured"] = False
                                            if "DHCP Binding IP addresses overlaps" in ''.join(result):
                                                self.orgVDCResult["DHCP Binding: Binding IP addresses overlaps with static IP Pool range"] = True
                                            else:
                                                self.orgVDCResult["DHCP Binding: Binding IP addresses overlaps with static IP Pool range"] = False
                                        if serviceName == "NAT":
                                            if "Nat64 rule is configured" in ''.join(result):
                                                self.orgVDCResult["NAT: NAT64 rule"] = True
                                            else:
                                                self.orgVDCResult["NAT: NAT64 rule"] = False
                                            if "Range of IPs or network found in this DNAT rule" in ''.join(result):
                                                self.orgVDCResult["NAT: Range of IPs or network in DNAT rule"] = True
                                            else:
                                                self.orgVDCResult["NAT: Range of IPs or network in DNAT rule"] = False
                                        if serviceName == "IPsec":
                                            if "routebased session type" in ''.join(result):
                                                self.orgVDCResult["IPsec: Route based session type"] = True
                                            else:
                                                self.orgVDCResult["IPsec: Route based session type"] = False
                                            if "unsupported encryption algorithm" in ''.join(result):
                                                self.orgVDCResult["IPsec: Unsupported Encryption Algorithm"] = True
                                            else:
                                                self.orgVDCResult["IPsec: Unsupported Encryption Algorithm"] = False
                                            if "CA certificate not found" in ''.join(result):
                                                self.orgVDCResult["IPsec: CA certificate is missing"] = True
                                            else:
                                                self.orgVDCResult["IPsec: CA certificate is missing"] = False
                                            if 'DNAT is not supported on a tier-1' in ''.join(result):
                                                self.orgVDCResult["IPsec: DNAT rules not supported with Policy-based session type"] = True
                                            else:
                                                self.orgVDCResult["IPsec: DNAT rules not supported with Policy-based session type"] = False
                                        if serviceName == "Routing":
                                            if "OSPF routing protocol" in ''.join(result):
                                                self.orgVDCResult["OSPF routing protocol"] = True
                                            else:
                                                self.orgVDCResult["OSPF routing protocol"] = False
                                            if "static routes configured" in ''.join(result):
                                                self.orgVDCResult['User-defined Static Routes'] = True
                                            else:
                                                self.orgVDCResult['User-defined Static Routes'] = False
                                        if serviceName == "L2VPN":
                                            if "L2VPN service is configured" in ''.join(result):
                                                self.orgVDCResult["L2VPN service"] = True
                                            else:
                                                self.orgVDCResult["L2VPN service"] = False
                                        if serviceName == "SSLVPN":
                                            if "SSLVPN service is configured" in ''.join(result):
                                                self.orgVDCResult["SSLVPN service"] = True
                                            else:
                                                self.orgVDCResult["SSLVPN service"] = False
                                        if serviceName == "Firewall":
                                            if "Any as a TCP/UDP port present" in ''.join(result):
                                                self.orgVDCResult["Gateway Firewall: Any as TCP/UDP port"] = True
                                            else:
                                                self.orgVDCResult["Gateway Firewall: Any as TCP/UDP port"] = False
                                            if "vNicGroupId" in ''.join(result):
                                                self.orgVDCResult["Gateway Firewall: Gateway Interfaces in rule"] = True
                                            else:
                                                self.orgVDCResult["Gateway Firewall: Gateway Interfaces in rule"] = False
                                            if "is connected to different edge gateway" in ''.join(result):
                                                self.orgVDCResult["Gateway Firewall: Networks connected to different edge gateway used"] = True
                                            else:
                                                self.orgVDCResult["Gateway Firewall: Networks connected to different edge gateway used"] = False
                                            if "grouping object type" in ''.join(result):
                                                self.orgVDCResult["Gateway Firewall: Unsupported grouping object"] = True
                                            else:
                                                self.orgVDCResult["Gateway Firewall: Unsupported grouping object"] = False
                                        if serviceName == 'Syslog':
                                            if 'Syslog service is configured' in ''.join(result):
                                                self.orgVDCResult["Syslog service"] = True
                                            else:
                                                self.orgVDCResult["Syslog service"] = False
                                        if serviceName == 'SSH':
                                            if 'SSH service is configured' in ''.join(result):
                                                self.orgVDCResult["SSH service"] = True
                                            else:
                                                self.orgVDCResult["SSH service"] = False
                                        if serviceName == "GRETUNNEL":
                                            if 'GRE tunnel is configured' in ''.join(result):
                                                self.orgVDCResult["GRE Tunnel"] = True
                                            else:
                                                self.orgVDCResult["GRE Tunnel"] = False
                                if desc == "Independent Disks":
                                    del self.orgVDCResult["Independent Disks"]
                                    diskResult = ''.join(output)
                                    self.orgVDCResult["Independent Disks: Shared disk present"] = (
                                        True
                                        if "Independent Disks in Org VDC are shared" in diskResult
                                        else False)
                                    self.orgVDCResult["Independent Disks: Attached VMs are not powered off"] = (
                                        True
                                        if "VMs attached to disks are not powered off" in diskResult
                                        else False)

                    except Exception as err:
                        self.logger.debug(f"Failed to evaluate Org VDC '{VDC}' of organization '{org}' due to error - '{str(err)}'")
                        self.orgVDCResult['Status'] = STATUS_CODES[3]
                        # Restoring log level of console logger
                        self.changeLogLevelForConsoleLog(disable=False)
                        self.consoleLogger.error(f"Failed to evaluate Org VDC '{VDC}' of organization '{org}'")
                        self.reportData.append(self.orgVDCResult)
                        continue

                    # Restore logging format
                    self.changeLoggingFormat(f"{VDC}:{org}", restore=True)

                    # Updating status of orgVDC assessment.
                    # Status of orgVDC assessment will updated based upon failed
                    # validations to respective STATUS_CODES
                    validation_severities = set(
                        VALIDATION_CLASSIFICATION[key]
                        for key, value in self.orgVDCResult.items()
                        if key in VALIDATION_CLASSIFICATION and value
                    )
                    if 2 in validation_severities:
                        self.orgVDCResult['Status'] = STATUS_CODES[2]
                    elif 1 in validation_severities:
                        self.orgVDCResult['Status'] = STATUS_CODES[1]

                    # Adding the data after validating to report data
                    self.reportData.append(self.orgVDCResult)

                    # Restoring log level of console logger
                    self.changeLogLevelForConsoleLog(disable=False)

                    self.consoleLogger.info(f"Successfully evaluated Org VDC '{VDC}' of organization '{org}'")
            # deleting the current user api session of vmware cloud director
            self.vcdValidationObj.deleteSession()
        except Exception:
            raise
        finally:
            # Restore logging format
            self.changeLoggingFormat(restore=True)

    def runV2TValidations(self, desc, method, args):
        """
        Description : Executes the validation method and arguments passed as parameters as stores exceptions raised
        Parameters : desc - Description of the method to be executed (STRING)
                     method - Reference of method (METHOD REFERENCE)
                     args - arguments passed to the method (LIST)
        """
        try:
            output = method(*args)
        except Exception as err:
            self.logger.debug(f"Error: {str(err)}")
            self.logger.debug(traceback.format_exc())
            self.orgVDCResult[desc] = True
        else:
            if output:
                self.logger.debug(f"Error: {output}")
            self.orgVDCResult[desc] = False
            return output

    def createReport(self):
        """
        Description : This method creates csv report for v2t-Assessment
        """
        try:
            # Writing detailed assessment report
            # Filename of detailed report file
            detailedReportfilename = os.path.join(self.reportBasePath,
                                                  f'{self.vcdUUID}-v2tAssessmentReport-{self.currentDateTime}.csv')

            # Writing data to detailed report csv
            with open(detailedReportfilename, "w", newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.reportData[0].keys())
                writer.writeheader()
                writer.writerows(self.reportData)

            # Writing summary data report
            # Filename of summary report file
            summaryReportfilename = os.path.join(self.reportBasePath,
                                                 f'{self.vcdUUID}-v2tAssessmentReport-Summary-{self.currentDateTime}.csv')

            # List that holds data for summary report
            summaryData = [["Summary", "Org VDCs", "VMs", "ORG VDC RAM (MB)"]]

            # Computation for org vdc/s data that can be migrated
            status_data = {
                status: {
                    'org_vdc_count': 0,
                    'vm_count': 0,
                    'org_vdc_ram': 0,
                }
                for status in STATUS_CODES.values()
            }
            for row in self.reportData:
                status_data[row['Status']]['org_vdc_count'] += 1
                status_data[row['Status']]['vm_count'] += int(row['VMs'])
                status_data[row['Status']]['org_vdc_ram'] += int(row['ORG VDC RAM (MB)'])

            # Adding data to summary data list
            for code, status in sorted(STATUS_CODES.items(), key=lambda x: x[0]):
                summaryData.append([
                    status,
                    status_data[status]['org_vdc_count'],
                    status_data[status]['vm_count'],
                    status_data[status]['org_vdc_ram'],
                ])

            # Performing computation for feature wise org vdc data
            summaryData.append([])
            summaryData.append(["Additional Info"])
            summaryData.append(["Validation Task", "Org VDCs", "VMs", "ORG VDC RAM (MB)", "Severity"])

            # Initializing feature dictionary. If any validation is not present in
            # VALIDATION_CLASSIFICATION, default code will be assigned
            feature_data = {
                feature: {
                    'org_vdc_count': 0,
                    'vm_count': 0,
                    'org_vdc_ram': 0,
                    'severity': VALIDATION_CODES[VALIDATION_CLASSIFICATION.get(
                        feature, NOT_CATEGORIZED_VALIDATION_CODE)]
                }
                for feature in list(
                    self.reportData[0].keys())[self.summaryColumnCount:]
            }
            for row in self.reportData:
                for feature, value in list(row.items())[self.summaryColumnCount:]:
                    if value and value != "NA":
                        feature_data[feature]['org_vdc_count'] += 1
                        feature_data[feature]['vm_count'] += int(row['VMs'])
                        feature_data[feature]['org_vdc_ram'] += int(row['ORG VDC RAM (MB)'])

            # Adding data to summary data list
            for feature, data in sorted(
                    feature_data.items(),
                    key=lambda x: VALIDATION_CLASSIFICATION.get(
                        x[0], NOT_CATEGORIZED_VALIDATION_CODE)):
                summaryData.append([
                    feature,
                    data['org_vdc_count'],
                    data['vm_count'],
                    data['org_vdc_ram'],
                    data['severity'],
                ])

            # Formatting time taken by v2tAssessment
            endTime = datetime.datetime.now()

            if "day" in str(endTime - self.initialTime):
                numberOfDays, timeTaken = f"{endTime - self.initialTime}".split(",")
                timeTaken = timeTaken.strip().split(":")
                timeFormat = f"{numberOfDays}" + \
                             (f" {timeTaken[0]} Hours" if int(timeTaken[0]) else str()) + \
                             (f" {timeTaken[1]} Minutes" if int(timeTaken[1]) else str()) + \
                             (f" {math.ceil(float(timeTaken[2]))} Seconds" if math.ceil(float(timeTaken[2])) else str())
            else:
                timeTaken = f"{endTime - self.initialTime}".split(":")
                timeFormat = (f"{timeTaken[0]} Hours" if int(timeTaken[0]) else str()) + \
                             (f" {timeTaken[1]} Minutes" if int(timeTaken[1]) else str()) + \
                             (f" {math.ceil(float(timeTaken[2]))} Seconds" if math.ceil(float(timeTaken[2])) else str())

            numberOfORGsEvaluated = len(set(row['Org Name'] for row in self.reportData))
            numberOfVDCsEvaluated = len([row['Org VDC'] for row in self.reportData])
            maximumNumberOfNetworksToBeBridged = max([row["Number of Networks to Bridge"] for row in self.reportData])

            # Adding the time stamps and summary data to summary report
            summaryData.insert(0, ["Build Version", self.buildVersion])
            summaryData.insert(1, ["VMware Cloud Director UUID", self.vcdUUID])
            summaryData.insert(2, ["VMware Cloud Director Version", self.vcdVersion])
            summaryData.insert(3, ["Start Time of V2T-Assessment", self.initialTime.strftime("%a,%d %B,%Y %I:%M:%S %p")])
            summaryData.insert(4, ["End Time of V2T-Assessment", endTime.strftime("%a,%d %B,%Y %I:%M:%S %p")])
            summaryData.insert(5, ["Total Time taken by V2T-Assessment", timeFormat])
            summaryData.insert(6, ["Number of Organization/s evaluated", numberOfORGsEvaluated])
            summaryData.insert(7, ["Number of Org VDC/s evaluated", numberOfVDCsEvaluated])
            summaryData.insert(8, ["Maximum Number of networks to be bridged in a single migration", maximumNumberOfNetworksToBeBridged])
            # Adding empty row to summary report
            summaryData.insert(9, [])

            # Creating object of pretty table class
            table = prettytable.PrettyTable(hrules=prettytable.ALL)
            # Adding title to table
            table.title = 'Execution Summary for V2T-Assessment'
            # Adding data to table
            table.field_names = ["Start Time of V2T-Assessment", self.initialTime.strftime("%a,%d %B,%Y %I:%M:%S %p")]
            table.add_row(["End Time of V2T-Assessment", endTime.strftime("%a,%d %B,%Y %I:%M:%S %p")])
            table.add_row(["Number of Organization/s evaluated", numberOfORGsEvaluated])
            table.add_row(["Number of Org VDC/s evaluated", numberOfVDCsEvaluated])
            table.add_row(["Total time taken by V2T-Assessment", f"{timeFormat}"])

            # Writing data to summary report csv
            with open(summaryReportfilename, "w", newline='') as f:
                writer = csv.writer(f)
                writer.writerows(summaryData)

            self.consoleLogger.warning(f"Detailed report path: {detailedReportfilename}")
            self.consoleLogger.warning(f"Summary report path: {summaryReportfilename}")

            # Logging the execution summary table
            self.consoleLogger.info('\n{}\n'.format(table.get_string()))

            self.consoleLogger.info('Successfully completed V2T-Assessment mode for NSX-V migration to NSX-T')
        except:
            raise
