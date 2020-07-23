########
# Copyright (c) 2020 Cloudify.co Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
############

import os

import yaml

from .. import env
from ..cli import cfy
from ..utils import get_dict_from_yaml
from ..exceptions import CloudifyCliError
from ..replace_certificates_config import (raise_errors_list,
                                           ReplaceCertificatesConfig)

CERTS_CONFIG_PATH = 'certificates_replacement_config.yaml'


@cfy.group(name='replace-certificates')
@cfy.options.common_options
def replace_certificates():
    """
    Handle the certificates replacement procedure
    """
    if not env.is_initialized():
        env.raise_uninitialized()


@replace_certificates.command(name='generate-config',
                              short_help='Generate the configuration file '
                                         'needed for certificates replacement')
@cfy.options.output_path
@cfy.assert_manager_active()
@cfy.pass_client()
@cfy.pass_logger
def get_replace_certificates_config_file(output_path,
                                         logger,
                                         client):
    # output_path is not a default param because of `cfy.options.output_path`
    output_path = output_path if output_path else CERTS_CONFIG_PATH
    config = _get_cluster_configuration_dict(client)
    with open(output_path, 'w') as output_file:
        yaml.dump(config, output_file, default_flow_style=False)

    logger.info('The certificates replacement configuration file was '
                'saved to %s', output_path)


@replace_certificates.command(name='start',
                              short_help='Replace certificates after updating '
                                         'the configuration file')
@cfy.options.input_path(help='The certificates replacement configuration file')
@cfy.options.force('Use the force flag in case you want to change only a '
                   'CA and not the certificates signed by it')
@cfy.assert_manager_active()
@cfy.pass_logger
def start_replace_certificates(input_path,
                               force,
                               logger):
    _validate_username_and_private_key()
    config_dict = get_dict_from_yaml(_get_input_path(input_path))
    logger.info('Validating replace-certificates config file...')
    validate_config_dict(config_dict, force, logger)

    main_config = ReplaceCertificatesConfig(config_dict, False, logger)
    main_config.validate_certificates()
    logger.info('\nReplacing certificates...')
    main_config.replace_certificates()
    new_cli_cert = main_config.new_cli_ca_cert()
    if new_cli_cert:
        env.profile.rest_certificate = new_cli_cert
    logger.info('\nSuccessfully replaced certificates')


def _get_input_path(input_path):
    input_path = input_path if input_path else CERTS_CONFIG_PATH
    if not os.path.exists(input_path):
        raise CloudifyCliError('Please create the replace-certificates '
                               'configuration file first using the command'
                               ' `cfy replace-certificates generate-file`')
    return input_path


def _get_cluster_configuration_dict(client):
    instances_ips = _get_instances_ips(client)
    return {
        'manager': {'cluster_members': [{
            'host_ip': str(host_ip),
            'new_internal_cert': '',
            'new_internal_key': '',
            'new_external_cert': '',
            'new_external_key': '',
            'new_postgresql_client_cert': '',
            'new_postgresql_client_key': ''
        } for host_ip in instances_ips['manager_ips']],
            'new_ca_cert': '',
            'new_external_ca_cert': '',
            'new_ldap_ca_cert': ''
        },
        'postgresql_server': {'cluster_members': [{
            'host_ip': str(host_ip),
            'new_cert': '',
            'new_key': ''
        } for host_ip in instances_ips['postgresql_ips']],
            'new_ca_cert': ''
        },
        'rabbitmq': {'cluster_members': [{
            'host_ip': str(host_ip),
            'new_cert': '',
            'new_key': ''
        } for host_ip in instances_ips['rabbitmq_ips']],
            'new_ca_cert': '',
        }

    }


def _get_instances_ips(client):
    return {'manager_ips': [manager.private_ip for manager in
                            client.manager.get_managers().items],
            'rabbitmq_ips': [broker.host for broker in
                             client.manager.get_brokers().items],
            'postgresql_ips': [db.host for db in
                               client.manager.get_db_nodes().items]
            }


def validate_config_dict(config_dict, force, logger):
    errors_list = []
    _validate_instances(errors_list, config_dict, force, logger)
    _check_path(errors_list, config_dict['manager']['new_ldap_ca_cert'])
    if errors_list:
        raise_errors_list(errors_list, logger)


def _validate_username_and_private_key():
    if (not env.profile.ssh_user) or (not env.profile.ssh_key):
        raise CloudifyCliError('Please configure the profile ssh-key and '
                               'ssh-user using the `cfy profiles set` command')


def _validate_instances(errors_list, config_dict, force, logger):
    for instance in 'postgresql_server', 'rabbitmq':
        _validate_cert_and_key(errors_list,
                               config_dict[instance]['cluster_members'])
        _validate_new_ca_cert(errors_list, config_dict, instance, force,
                              logger)

    _validate_manager_cert_and_key(errors_list,
                                   config_dict['manager']['cluster_members'])
    _validate_new_manager_ca_certs(errors_list, config_dict, force, logger)


def _validate_new_ca_cert(errors_list, config_dict, instance_name, force,
                          logger):
    _validate_ca_cert(errors_list, config_dict[instance_name], instance_name,
                      'new_ca_cert', 'new_cert',
                      config_dict[instance_name]['cluster_members'],
                      force, logger)


def _validate_new_manager_ca_certs(errors_list, config_dict, force, logger):
    _validate_ca_cert(errors_list, config_dict['manager'], 'manager',
                      'new_ca_cert', 'new_internal_cert',
                      config_dict['manager']['cluster_members'],
                      force, logger)
    _validate_ca_cert(errors_list, config_dict['manager'],
                      'manager', 'new_external_ca_cert',
                      'new_external_cert',
                      config_dict['manager']['cluster_members'],
                      force, logger)
    _validate_ca_cert(errors_list, config_dict['postgresql_server'],
                      'postgresql_server', 'new_ca_cert',
                      'new_postgresql_client_cert',
                      config_dict['manager']['cluster_members'],
                      force, logger)


def _validate_ca_cert(errors_list, instance, instance_name, new_ca_cert_name,
                      cert_name, cluster_members, force, logger):
    """Validates the CA cert.

    Validates that the CA path is valid, and if it is, then a new cert was
    specified for all cluster members.
    """
    err_msg = '{0} was specified for instance {1}, but {2} was not specified' \
              ' for all cluster members.'.format(new_ca_cert_name,
                                                 instance_name,
                                                 cert_name)

    new_ca_cert_path = instance.get(new_ca_cert_name)
    if _check_path(errors_list, new_ca_cert_path):
        if not all(member.get(cert_name) for member in cluster_members):
            if force:
                logger.info(err_msg)
            else:
                errors_list.append(err_msg +
                                   ' Please use `--force` if you still wish '
                                   'to replace the certificates')


def _validate_cert_and_key(errors_list, nodes):
    for node in nodes:
        _validate_node_certs(errors_list, node, 'new_cert',
                             'new_key')


def _validate_manager_cert_and_key(errors_list, nodes):
    for node in nodes:
        _validate_node_certs(errors_list, node,
                             'new_internal_cert',
                             'new_internal_key')
        _validate_node_certs(errors_list, node,
                             'new_external_cert',
                             'new_external_key')
        _validate_node_certs(errors_list, node,
                             'new_postgresql_client_cert',
                             'new_postgresql_client_key')


def _validate_node_certs(errors_list, certs_dict, new_cert_name, new_key_name):
    new_cert_path = certs_dict.get(new_cert_name)
    new_key_path = certs_dict.get(new_key_name)
    if bool(new_key_path) != bool(new_cert_path):
        errors_list.append('Either both {0} and {1} must be '
                           'provided, or neither for host '
                           '{2}'.format(new_cert_name, new_key_name,
                                        certs_dict['host_ip']))
    _check_path(errors_list, new_cert_path)
    _check_path(errors_list, new_key_path)


def _check_path(errors_list, path):
    if path:
        if os.path.exists(path):
            return True
        errors_list.append('The path {0} does not exist'.format(path))
    return False