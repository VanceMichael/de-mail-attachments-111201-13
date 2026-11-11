import base64
import hashlib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser


def make_message(subject, name, content):
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = 'supplier@example.invalid'
    message['To'] = 'archive@example.invalid'
    message.set_content('合作资料附件')
    message.add_attachment(content, maintype='text', subtype='csv', filename=name)
    return message.as_bytes()


def attachments(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result = []
    for position, part in enumerate(message.walk()):
        if part.get_content_disposition() != 'attachment':
            continue
        content = part.get_payload(decode=True)
        if content is None:
            raise ValueError('附件无法解码')
        result.append(dict(part=position, filename=part.get_filename(),
                           digest=hashlib.sha256(content).hexdigest(), content=content))
    return result


def collect(payload):
    raw = make_message(payload['subject'], payload['filename'], payload['csv'].encode('utf-8'))
    return [dict(key=item['digest'], filename=item['filename'], part=item['part'],
                 size=len(item['content'])) for item in attachments(raw)]
