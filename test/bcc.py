#!/usr/bin/python3

import sys
import os
import time
import shutil
import tempfile
import configparser
import getpass
import wtfexpect

def postgres(we, host, port, datadir):
	name = 'postgres %s' % datadir
	we.spawn(name,
		'postgres',
		'-i',
		'-h', host,
		'-p', str(port),
		'-D', datadir,
	)
	return name

def postgri(we, hosts, ports, datadirs):
	names = []
	for host, port, datadir in zip(hosts, ports, datadirs):
		name = postgres(we, host, port, datadir)
		names.append(name)
	return names

def initdbs(we, datadirs):
	for d in datadirs:
		name = 'initdb %s' % d
		we.spawn(name, 'initdb', d)

	ok = True
	while we.alive():
		name, line = we.expect({})
		assert(line is None)
		retcode = we.getcode(name)
		if retcode != 0:
			ok = False
	return ok

def pgbouncer(we, name, host, port, hosts, ports, database, user):
	assert(len(hosts) > 0)
	assert(len(ports) == len(hosts))

	connstr = "host=%s port=%d" % (hosts[0], ports[0])
	connstr += " dbname=%s" % database
	connstr += " user=%s" % user
	for h, p in list(zip(hosts, ports))[1:]:
		connstr += " bcc_host=%s bcc_port=%d" % (h, p)

	cfg = configparser.ConfigParser()
	cfg['databases'] = {
		'postgres': connstr,
	}
	cfg['pgbouncer'] = {
		'bcc_buffer': 1024 * 1024 * 2,
		'listen_port': port,
		'listen_addr': host,
		'auth_type': 'any',
		'logfile': '/tmp/pgbouncer.log',
	}

	#confile = tempfile.NamedTemporaryFile(mode='w+')
	fd, confilename = tempfile.mkstemp()
	confile = os.fdopen(fd, 'w+')
	cfg.write(confile)
	confile.flush()

	return we.spawn(name, './pgbouncer', confilename)

def pgbench(we, name, host, port, database, user, jobs=5, clients=5, seconds=5, init=False):
	params = [
		'-h', host,
		'-p', str(port),
		'-U', user,
	]
	if init:
		params.append('-i')
	else:
		params.extend([
			'-j', str(jobs),
			'-c', str(clients),
			'-T', str(seconds),
		])
	params.append(database)
	return we.spawn(name, 'pgbench', *params)

def psql(we, name, host, port, database, user, cmd):
	return we.spawn(name,
		'psql',
		'-h', host,
		'-p', str(port),
		'-U', user,
		'-c', cmd,
		database,
	)

# You have to add yourself to sudoers to be able to use iptables:
#   <username> ALL = (root) NOPASSWD: /sbin/iptables
rules = []
def iptables_block_port(we, port):
	rule = [
		'INPUT',
		'-p', 'tcp',
		'--dport', str(port),
		'-j', 'DROP',
	]
	rc, out = we.run(['sudo', 'iptables', '-A'] + rule)
	if rc == 0:
		rules.append(rule)
	else:
		raise Exception("failed to add iptables rule: %s" % out)
	return rc == 0

def iptables_cleanup(we):
	for rule in rules:
		rc, out = we.run(['sudo', 'iptables', '-D'] + rule)
	rules.clear()

def equal_results(we, names):
	results = we.capture(*names)
	retcodes = [x['retcode'] for x in results.values()]
	outputs = [x['output'] for x in results.values()]
	if any(rc != 0 for rc in retcodes):
		return False, results
	if any(out != outputs[0] for out in outputs):
		return False, results
	return True, outputs[0]

def has_one_hole(log, sublog):
	ok_from_top = 0
	ok_from_bottom = 0

	for a, b in zip(list(log), list(sublog)):
		with open('/tmp/direct', 'w') as f:
			f.write('%s:%s\n' % (a, b))

		if a == b:
			ok_from_top += 1
		else:
			break

	for a, b in zip(reversed(log), reversed(sublog)):
		with open('/tmp/reversed', 'w') as f:
			f.write('%s:%s\n' % (a, b))

		if a == b:
			ok_from_bottom += 1
		else:
			break

	return ok_from_top + ok_from_bottom >= len(sublog)

def main():
	datadirs = []
	daemons = []

	host = '127.0.0.1'
	port = 5432
	bcc_host = '127.0.0.1'
	bcc_port = 5433
	bouncer_port = 6543
	database = 'postgres'
	user = getpass.getuser()
	bench_seconds = 160
	bench_jobs = 1
	bench_clients = bench_jobs

	we = wtfexpect.WtfExpect()

	ok = False

	try:
		# --------- prepare

		datadirs.append(tempfile.mkdtemp())
		datadirs.append(tempfile.mkdtemp())

		print("initdb")
		if not initdbs(we, datadirs):
			raise Exception("failed to initialize databases")

		print("launch postgres")
		notready = postgri(we, [host, bcc_host], [port, bcc_port], datadirs)
		daemons.extend(notready)
		while len(notready) > 0:
			name, line = we.readline(timeout=1)
			if name in notready and 'database system is ready to accept connections' in line:
				print("%s ready" % name)
				notready.remove(name)

		print("launch pgbouncer")
		daemons.append(pgbouncer(
			we, 'pgbouncer', host, bouncer_port,
			[host, bcc_host], [port, bcc_port],
			database, user,
		))

		print("wait until pgbouncer gets up")
		while we.alive('pgbouncer'):
			name, line = we.readline(timeout=1)
			if line is not None:
				print("[%s] %s" % (name, line))
			if name == 'pgbouncer' and 'process up' in line:
				break

		# --------- bench

		print("bench init")
		pgbench(we, 'pgbench', host, bouncer_port, database, user, init=True)
		while we.alive('pgbench'):
			name, line = we.readline(timeout=1)
			if line is not None:
				print("[%s] %s" % (name, line))
		if we.getcode('pgbench') != 0:
			raise Exception("pgbench -i failed")

		print("launch bench %d sec" % bench_seconds)
		pgbench(we, 'pgbench', host, bouncer_port, database, user, seconds=bench_seconds, jobs=bench_jobs, clients=bench_clients)

		print("wait 3 sec")
		name, line = we.expect({}, timeout=3)
		if name is not None:
			if line is None:
				raise Exception("has one of the daemons (%s) finished?" % name)

		print("block port %s" % bcc_port)
		iptables_block_port(we, bcc_port)

		print("wait until bcc connection gags")
		while we.alive('pgbench'):
			name, line = we.readline(timeout=1)
			if name is None:
				continue
			if line is None:
				continue
			print("[%s] %s" % (name, line))
			if 'useless' in line:
				break

		print("unblock ports")
		iptables_cleanup(we)

		print("wait for bench to finish")
		while we.alive('pgbench'):
			name, line = we.readline(timeout=1)
			if name is None:
				continue
			if line is None:
				continue
			print("[%s] %s" % (name, line))

		print("wait 3 sec")
		name, line = we.expect({'pgbouncer': ''}, timeout=3)
		if name is not None:
			raise Exception("has one of the daemons (%s) finished?" % name)

		# --------- check
		print("unblock ports")
		iptables_cleanup(we)

		print("check")
		psqls = []
		for h, p in [(host, port), (bcc_host, bcc_port)]:
			name = 'psql-%d' % p
			psql(
				we, name, h, p, database, user,
				'''
				select tid, bid, aid, delta
				from pgbench_history
				order by mtime
				''',
			)
#			psql(
#				we, name, h, p, database, user,
#				'''
#				select tid, bid, aid, delta
#				from pgbench_history
#				order by tid, bid, aid, delta
#				''',
#			)
			psqls.append(name)
		equal, result = equal_results(we, psqls)
		if equal:
			print("results are equal: %s" % result[-2])
			ok = True
		else:
			print("results not equal")
			for name, res in result.items():
				filename = '/tmp/%s.output' % name
				with open(filename, 'w') as f:
					f.write('\n'.join(list(res['output'])))
					print("see %s" % filename)
			log = result[psqls[0]]['output']
			sublog = result[psqls[1]]['output']
			if has_one_hole(log[:-2], sublog[:-2]):
				print("but one hole in the middle is ok")
				ok = True

	finally:
		# --------- cleanup

		print("cleanup")
		iptables_cleanup(we)
		we.finish()
		for d in datadirs:
			shutil.rmtree(d)

	if ok:
		print("ok")
		sys.exit(0)
	else:
		print("FAILED")
		sys.exit(1)

if __name__ == '__main__':
	main()
