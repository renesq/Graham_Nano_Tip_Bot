from io import BytesIO
from celery import Celery
from celery.utils.log import get_task_logger

import redis
import json
import settings
import pycurl

# TODO (besides test obvi)
# - receive logic

logger = get_task_logger(__name__)

r = redis.StrictRedis()
app = Celery('graham', broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')
app.conf.CELERY_MAX_CACHED_RESULTS = -1

def communicate_wallet(wallet_command):
	buffer = BytesIO()
	c = pycurl.Curl()
	c.setopt(c.URL, '[::1]')
	c.setopt(c.PORT, 7076)
	c.setopt(c.POSTFIELDS, json.dumps(wallet_command))
	c.setopt(c.WRITEFUNCTION, buffer.write)
	c.setopt(c.TIMEOUT, 300)
	c.perform()
	c.close()

	body = buffer.getvalue()
	parsed_json = json.loads(body.decode('iso-8859-1'))
	return parsed_json

@app.task(bind=True, max_retries=10)
def send_transaction(self, tx):
    try:
        source_address = tx['source_address']
        to_address = tx['to_address']
        amount = tx['amount']
        uid = tx['uid']
        attempts = tx['attempts']
        raw_withdraw_amt = str(amount) + '000000000000000000000000'
        wallet_command = {
            'action': 'send',
            'wallet': settings.wallet,
            'source': source_address,
            'destination': to_address,
            'amount': int(raw_withdraw_amt),
            'id': uid
        }
        logger.debug("RPC Send")
        wallet_output = communicate_wallet(wallet_command)
        logger.debug("RPC Response")
        if 'block' in wallet_output:
            txid = wallet_output['block']
            # Also pocket these timely
            logger.info("Pocketing tip for %s, block %s", to_address, txid)
            pocket_tx(to_address, txid)
            r.rpush('/send_finished', self.request.id)
            return {"success": {"source":source_address, "txid":txid, "uid":uid, "destination":to_address, "amount":amount}}
        else:
            self.retry()
    except pycurl.error:
        self.retry()
    except Exception as e:
        # Just log these because i'm not sure offhand what other types of exceptions
        # we may get here
        logger.exception(e)
        self.retry()

def pocket_tx(account, block):
	action = {
		"action":"receive",
		"wallet":settings.wallet,
		"account":account,
		"block":block
	}
	return communicate_wallet(action)

@app.task
def pocket_task(accounts):
	processed_count = 0
	try:
		accts_pending_action = {
			"action":"accounts_pending",
			"accounts":accounts,
			"threshold":1000000000000000000000000,
			"count":5
		}
		resp = communicate_wallet(accts_pending_action)
		if resp is None or 'blocks' not in resp:
			return None
		for account, blocks in resp['blocks'].items():
			for b in blocks:
				logger.info("Receiving block %s for account %s", b, account)
				rcv_resp = pocket_tx(account, b)
				if rcv_resp is None or 'block' not in rcv_resp:
					logger.info("Couldn't receive %s - response: %s", b, str(rcv_resp))
				else:
					processed_count += 1
					logger.info("pocketed block %s", b)
	except Exception as e:
		logger.exception(e)
		return None
	return processed_count

if __name__ == '__main__':
	app.start()
