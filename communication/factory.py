from .modbus import ModbusCommunicator
from .openshowvar import OpenShowVarCommunicator
from .simulation import SimulationCommunicator
from .profinet import CifXCommunicator


def create_communicator(config):
    mode = config['communication']['mode']
    if mode == 'modbus':
        return ModbusCommunicator(config['communication']['modbus'])
    elif mode == 'openshowvar':
        return OpenShowVarCommunicator(config['communication']['openshowvar'])
    elif mode == 'profinet':
        return CifXCommunicator(config['communication']['profinet'].get('device_name', 'cifX0'))
    elif mode == 'simulation':
        return SimulationCommunicator(config['communication'])
    else:
        raise ValueError(f"Unsupported mode: {mode}")