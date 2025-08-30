import ssl
import time
import os
import datetime
import email
import smtplib
import threading
import traceback
import re
import base64
import quopri
import signal
import logging
import sys
import socket
import struct

import imapclient
import configparser
import humanfriendly

class Getmail(threading.Thread):

    def __init__(self, configparser_file, config_name):
        threading.Thread.__init__(self)
        self.event = threading.Event()
        #self.configparser_file = configparser_file
        #self.config_name = config_name
        #self.setName("Thread-%s" % config_name)
        self.name = "Thread-%s" % config_name
        self.imap = None
        self.exit_imap_idle_loop = False
        self.exception_counter = 0
        self.print_lock = threading.Lock()
        self.last_renew_imap_idle_connection = time.monotonic()
        self.idle_check_timeout = 1

        self.imap_hostname        = configparser_file.get(       config_name, 'imap_hostname')
        self.imap_port            = configparser_file.getint(    config_name, 'imap_port')
        self.imap_ssl             = configparser_file.getboolean(config_name, 'imap_ssl', fallback=True)
        self.imap_username        = configparser_file.get(       config_name, 'imap_username')
        self.imap_password        = configparser_file.get(       config_name, 'imap_password')
        self.imap_move_folder     = configparser_file.get(       config_name, 'imap_move_folder', fallback="getmail")
        self.imap_sync_folder     = configparser_file.get(       config_name, 'imap_sync_folder', fallback="INBOX")
        self.imap_move_enable     = configparser_file.getboolean(config_name, 'imap_move_enable', fallback=False)
        self.imap_debug           = configparser_file.getboolean(config_name, 'imap_debug', fallback=False)

        self.send_protocol        = configparser_file.get(       config_name, 'send_protocol', fallback="lmtp")

        self.lmtp_hostname        = configparser_file.get(       config_name, 'lmtp_hostname', fallback="localhost")
        self.lmtp_port            = configparser_file.getint(    config_name, 'lmtp_port', fallback=24)
        self.lmtp_recipient       = configparser_file.get(       config_name, 'lmtp_recipient', fallback="local@localhost")
        self.lmtp_debug           = configparser_file.getboolean(config_name, 'lmtp_debug', fallback=False)

        self.smtp_hostname        = configparser_file.get(       config_name, 'smtp_hostname', fallback="localhost")
        self.smtp_port            = configparser_file.getint(    config_name, 'smtp_port', fallback=25)
        self.smtp_recipient       = configparser_file.get(       config_name, 'smtp_recipient', fallback="local@localhost")
        self.smtp_debug           = configparser_file.getboolean(config_name, 'smtp_debug', fallback=False)

        self.clamd_active         = configparser_file.getboolean(config_name, 'clamd_active', fallback=False)
        self.clamd_hostname       = configparser_file.get(       config_name, 'clamd_hostname', fallback="clamd")
        self.clamd_port           = configparser_file.getint(    config_name, 'clamd_port', fallback=3310)
        self.clamd_max_chunk_size = humanfriendly.parse_size(configparser_file.get(config_name, 'clamd_max_chunk_size', fallback="10MB"), binary=True) # MUST be < StreamMaxLength in /etc/clamav/clamd.conf

    def run(self):
        while not self.exit_imap_idle_loop:
          try:
            self.event.wait(5)
            self.imap_idle()
          except Exception as e:
            logging.error("ERROR: %s" % (e))
            #traceback.print_exc()

          if not self.exit_imap_idle_loop:
            self.exception_counter += 1
            logging.error("ERROR: restart thread in %s minutes (counter: %d)" % (self.exception_counter * self.exception_counter, self.exception_counter))
            self.event.wait(60 * self.exception_counter * self.exception_counter )

    def imap_idle_stop(self):
        logging.info("IMAP_IDLE_STOP")
        self.exit_imap_idle_loop = True
        self.event.set()

    def imap_start_connection(self):
        logging.info("Start Getmail - server: %s:%s, username: %s, ssl: %s" % (self.imap_hostname, self.imap_port, self.imap_username, self.imap_ssl))

        self.imap = imapclient.IMAPClient(self.imap_hostname, port=self.imap_port, ssl=self.imap_ssl, use_uid=True)
        login_status = self.imap.login(self.imap_username, self.imap_password).decode("utf-8")
        logging.info("Login - status: %s" % login_status)

        if not self.imap.has_capability('IDLE'):
            logging.error("Server doesn't support IDLE!!")
            sys.exit()

#        if self.imap_debug:
#          self.imap.debug = True
#          logging.basicConfig(level=logging.DEBUG)
#        else:
#          self.imap.debug = False
#          logging.basicConfig(level=logging.INFO)

        if not self.imap.folder_exists(self.imap_sync_folder):
          status =  self.imap.create_folder(self.imap_sync_folder)
          logging.info("imap_sync_folder (%s) create status: %s " % (self.imap_sync_folder, status))

        self.imap.select_folder(self.imap_sync_folder)

        self.exception_counter = 0


    def imap_close_connection(self):
        if self.imap != None:
          status_logout = self.imap.logout()
          #logging.info("Close IMAP connection - status_logout: %s" % (status_logout))

    def imap_idle(self):
        self.imap_start_connection()
        self.create_imap_move_folder()
        logging.info("IMAP fetch mail - initial")
        self.imap_fetch_mail()

        # Start IDLE mode
        self.imap.idle()

        logging.info("Join infinite loop and wait for new mails, cancel with Ctrl-c")
        while not self.exit_imap_idle_loop:

            # Wait for up to x seconds for an IDLE response
            # https://imapclient.readthedocs.io/en/2.1.0/advanced.html
            start_time_idle_check = time.monotonic()
            responses = self.imap.idle_check(timeout=self.idle_check_timeout)
            execution_time_idle_check = time.monotonic() - start_time_idle_check

            self.check_imap_idle_response(responses, execution_time_idle_check)

        # End IDLE mode
        self.imap.idle_done()
        self.imap_close_connection()

    def check_imap_idle_response(self, responses, execution_time_idle_check):
        #https://tools.ietf.org/html/rfc3501#page-71

        self.renew_imap_idle_connection()

        if responses == []:
            if (execution_time_idle_check < self.idle_check_timeout / 2):
              #logging.info("TEST -- IMAP IDLE response: %s " % responses)
              raise Exception('idle_check responded too quickly, something is wrong with the IMAP Idle connection (execution_time_idle_check: %s' % execution_time_idle_check)
            else:
              # default action, when everything is ok
              return
        elif responses == None:
            return
        elif responses == [(b'OK', b'Still here')]:
            return
        elif responses == [(b'BYE', b'timeout')]:
            raise Exception('IMAP Connection Timeout, restart connection')

        logging.debug("IMAP IDLE response: %s " % responses)
        for item in responses:
            if len(item) == 2:
              if item[1] == b'EXISTS':
                self.imap.idle_done()
                self.imap_fetch_mail()
                self.imap.idle()


    def renew_imap_idle_connection(self):
        # https://tools.ietf.org/html/rfc2177
        # Because of that, clients using IDLE are advised to terminate the IDLE and
        # re-issue it at least every 29 minutes to avoid being logged off.
        # but 13 minutes are a sweet spot
        max_idle_time = 13*60

        if time.monotonic() - self.last_renew_imap_idle_connection > max_idle_time:
            self.last_renew_imap_idle_connection = time.monotonic()
            logging.debug("renew imap idle session")
            self.imap.idle_done()
            self.imap.idle()
            self.check_imap_idle_response_counter_between_renew = 0

    def imap_fetch_mail(self):
        #https://github.com/mjs/imapclient/blob/011748fd687c43636a8ef2c3acb9fa85782b91bc/examples/email_parsing.py
        messages = self.imap.search(criteria=u'ALL')
        for uid, message_data in self.imap.fetch(messages, 'RFC822').items():
          email_message = email.message_from_bytes(message_data[b'RFC822'])
          #logging.info("%s,%s,%s" % (uid, email_message.get('From'), email_message.get('Subject')) )

          # Scan the attachments for viruses
          virus_found = False
          if self.clamd_active:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
              #Connect to ClamAV
              try:
                s.connect((self.clamd_hostname, self.clamd_port))
              except ConnectionRefusedError as e:
                logging.error("Clamd (ConnectionRefusedError): %s" % (e))
                return False
              except socket.gaierror as e:
                logging.error("Clamd (Server is not reachable): %s" % (e))
                return False
              #Send mail to check
              try:
                email_bytes = email_message.as_bytes()
                #Send start frame
                s.send(b'nINSTREAM\n')
                # Calculate total length
                total_length = len(email_bytes)
                # Loop through each chunk and send with its length
                for i in range(0, total_length, self.clamd_max_chunk_size):
                  chunk = email_bytes[i:i+self.clamd_max_chunk_size]
                  chunk_length = struct.pack(b'!L', len(chunk))
                  s.send(chunk_length + chunk)
                #Send end frame
                s.send(struct.pack(b'!L', 0))
                #Get result
                result = s.recv(4096).decode("utf-8").strip()
              except (socket.error, BrokenPipeError) as e:
                logging.error(f"Error sending attachment to ClamAV: {e}")
                return False
              if result != None and "OK" in result:
                virus_found = False
              elif result != None and "FOUND" in result:
                logging.info("%s,%s,%s, %s" % (uid, email_message.get('From'), email_message.get('Subject'), result) + " |Email infected with virus, not sending." )
                virus_found = True
              else:
                logging.error("%s,%s,%s, %s" % (uid, email_message.get('From'), email_message.get('Subject'), result) + " |Error ClamAV." )
                return False

          # Send mail if ok
          if virus_found:
            self.imap_delete_mail(uid)
          else:
            if self.deliver_mail(email_message):
              if self.imap_move_enable:
                self.imap_move_mail(uid)
              else:
                self.imap_delete_mail(uid)

    def imap_delete_mail(self, uid):
        self.imap.delete_messages([uid])
        self.imap.expunge()
        logging.info('IMAP delete: delete email (uid: %s)' % str(uid) )

    def create_imap_move_folder(self):
        if self.imap_move_enable:
          if self.imap.folder_exists(self.imap_move_folder):
            logging.info("imap_move_folder (%s) already exists, nothing to do." % (self.imap_move_folder))
          else:
            status =  self.imap.create_folder(self.imap_move_folder)
            logging.info("imap_move_folder (%s) create status: %s " % (self.imap_move_folder, status))


    def imap_move_mail(self, uid):
        self.imap.move(uid, self.imap_move_folder)
        logging.info('IMAP move: move email to imap_move_folder (%s)' % (self.imap_move_folder) )

    def deliver_mail(self, email_message):
        result = False
        # select deliver protocol
        if self.send_protocol == "lmtp":
          result = self.lmtp_deliver_mail(email_message)
        elif self.send_protocol == "smtp":
          result = self.smtp_deliver_mail(email_message)
        else:
          logging.error("Email deliver (Exception - send_message; no protocol selected)")
        return result

    def lmtp_deliver_mail(self, email_message):
        logging.info( "LMTP deliver: start -- LMTP host: %s:%s" % (self.lmtp_hostname, self.lmtp_port))
        try:
          try:
            lmtp = smtplib.LMTP(self.lmtp_hostname, self.lmtp_port)
          except ConnectionRefusedError as e:
            logging.error("LMTP deliver (ConnectionRefusedError): %s" % (e))
            return False
          except socket.gaierror as e:
            logging.error("LMTP deliver (LMTP-Server is not reachable): %s" % (e))
            return False

          if self.lmtp_debug:
            lmtp.set_debuglevel(1)

          email_message['X-getmail-retrieved-from-mailbox-user'] = self.imap_username
          #email_message['X-getmail-retrieved-from-mailbox-folder'] = self.imap_sync_folder

          try:
            lmtp.send_message(email_message, to_addrs=self.lmtp_recipient)
          except Exception as e:
            logging.error("LMTP deliver (Exception - send_message #1): %s" % (e))
            traceback.print_exc()

            try:
              email_from = email_message.get('From')
              lmtp.send_message(email_message, from_addr=email_from, to_addrs=self.lmtp_recipient)
            except Exception as e:
              logging.error("LMTP deliver (Exception - send_message #2): %s" % (e))
              return False

            #return False
          finally:
            lmtp.quit()

          try:
            email_from_decoded    = email.header.make_header(email.header.decode_header(email_message.get('From')))
            email_subject_decoded = email.header.make_header(email.header.decode_header(email_message.get('Subject')))
            #logging.info(u'LMTP deliver: new eMail from: [%s], subject: [%s] ----> LMTP recipient: %s' % (email_from_decoded, email_subject_decoded, self.lmtp_recipient))
            logging.info(u'LMTP deliver: new eMail from: [%s], subject: [%s]' % (email_from_decoded, email_subject_decoded))
          except Exception as e:
            logging.error("LMTP deliver (Exception - decode error): %s" % (e))
            #logging.info(u'LMTP deliver: new eMail ----> LMTP recipient: %s' % (self.lmtp_recipient))
            logging.info(u'LMTP deliver: new eMail')
          return True

        except Exception as e:
          logging.error("LMTP deliver (Exception): %s" % (e))
          logging.error(traceback.format_exc())
          return False

    def smtp_deliver_mail(self, email_message):
        logging.info( "SMTP deliver: start -- SMTP host: %s:%s" % (self.smtp_hostname, self.smtp_port))
        try:

          try:
            smtp = smtplib.SMTP(self.smtp_hostname, self.smtp_port)
          except ConnectionRefusedError as e:
            logging.error("SMTP deliver (ConnectionRefusedError): %s" % (e))
            return False
          except socket.gaierror as e:
            logging.error("SMTP deliver (SMTP-Server is not reachable): %s" % (e))
            return False

          if self.smtp_debug:
            smtp.set_debuglevel(1)

          email_message['X-getmail-retrieved-from-mailbox-user'] = self.imap_username

          smtp.ehlo()
          smtp.starttls()
          smtp.ehlo()

          try:
            #https://docs.python.org/3/library/smtplib.html#smtplib.SMTP.send_message
            smtp.send_message(email_message, to_addrs=self.smtp_recipient)
          except Exception as e:
            logging.error("SMTP deliver (Exception - send_message #1): %s" % (e))
            traceback.print_exc()

            try:
              email_from = email_message.get('From')
              smtp.send_message(email_message, from_addr=email_from, to_addrs=self.smtp_recipient)
            except Exception as e:
              logging.error("SMTP deliver (Exception - send_message #2): %s" % (e))
              return False

          finally:
            smtp.quit()

          try:
            email_from_decoded    = email.header.make_header(email.header.decode_header(email_message.get('From')))
            email_subject_decoded = email.header.make_header(email.header.decode_header(email_message.get('Subject')))
            #logging.info(u'SMTP deliver: new eMail from: [%s], subject: [%s] ----> SMTP recipient: %s' % (email_from_decoded, email_subject_decoded, self.smtp_recipient))
            logging.info(u'SMTP deliver: new eMail from: [%s], subject: [%s]' % (email_from_decoded, email_subject_decoded))
          except Exception as e:
            logging.error("SMTP deliver (Exception - decode error): %s" % (e))
            #logging.info(u'SMTP deliver: new eMail ----> SMTP recipient: %s' % (self.smtp_recipient))
            logging.info(u'SMTP deliver: new eMail')

          return True

        except Exception as e:
          logging.error("SMTP deliver (Exception): %s" % (e))
          logging.error(traceback.format_exc())
          return False

########################################################################################################################
########################################################################################################################
########################################################################################################################


def start_getmail():

  configparser_file = get_configparser_file()
  all_connections = {}

  for config_name in configparser_file.sections():
        all_connections[config_name] = Getmail(configparser_file, config_name)
        all_connections[config_name].start()

  try:
    exit_program = False
    while not exit_program:
      try:
        signal.pause()
      except KeyboardInterrupt:
        exit_program = True
      except Exception as e:
        logging.error("ERROR: %s" % (e))
        traceback.print_exc()
  finally:
    logging.info("START: shutdown all IMAP connections")
    for config_name in all_connections:
      all_connections[config_name].imap_idle_stop()
    for config_name in all_connections:
      all_connections[config_name].join()
    logging.info("END: shutdown all IMAP connections")


def get_configparser_file():

  if os.path.isfile("./settings.ini"):
    config_file_path = "./settings.ini"
  else:
    logging.error("ERROR settings.ini not found!")
    return

  logging.info("use config file: %s" % config_file_path)
  configparser_file = configparser.ConfigParser(interpolation=None)
  configparser_file.read([os.path.abspath(config_file_path)])

  return configparser_file

def exit_gracefully(signum, frame):
    logging.info("Caught signal %d" % signum)
    raise KeyboardInterrupt

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  exit_gracefully)
    signal.signal(signal.SIGTERM, exit_gracefully)

    logging.basicConfig(
      format='%(asctime)s - %(threadName)s - %(levelname)s: %(message)s',
      level=logging.INFO
    )

    start_getmail()
