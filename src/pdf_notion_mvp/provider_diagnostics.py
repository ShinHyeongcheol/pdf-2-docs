"""Extract bounded public error metadata; never retain provider messages or headers."""
import json
import re


STATUSES = frozenset({
    'INVALID_ARGUMENT', 'RESOURCE_EXHAUSTED', 'PERMISSION_DENIED',
    'UNAUTHENTICATED', 'NOT_FOUND', 'FAILED_PRECONDITION', 'INTERNAL', 'UNAVAILABLE',
})
REASONS = frozenset({
    'API_KEY_INVALID', 'API_KEY_EXPIRED', 'API_KEY_SERVICE_BLOCKED',
    'API_KEY_HTTP_REFERRER_BLOCKED', 'API_KEY_IP_ADDRESS_BLOCKED',
    'API_KEY_ANDROID_APP_BLOCKED', 'API_KEY_IOS_APP_BLOCKED',
    'SERVICE_DISABLED', 'BILLING_DISABLED', 'RATE_LIMIT_EXCEEDED',
    'CONSUMER_INVALID', 'PROJECT_INVALID',
})
QUOTA_METRICS = frozenset('generativelanguage.googleapis.com/' + suffix for suffix in (
    'generate_content_free_tier_requests', 'generate_content_free_tier_input_token_count',
    'generate_content_paid_tier_requests', 'generate_content_paid_tier_input_token_count',
    'generate_content_requests', 'generate_content_input_token_count',
))


def _field_path(value, request, secret):
    """Only public field identifiers from the approved request, never arbitrary text."""
    if not isinstance(value, str) or len(value) > 200 or secret and secret in value:
        return None
    if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_.$\[\]"\'-]*', value):
        return None
    normalize = lambda name: name.replace('_', '').lstrip('$').casefold()
    names = {'model', 'responseschema'}
    def collect(obj):
        if isinstance(obj, dict):
            for name, child in obj.items():
                names.add(normalize(name))
                collect(child)
        elif isinstance(obj, list):
            for child in obj:
                collect(child)
    collect(request)
    tokens = [normalize(t) for t in re.findall(r'[A-Za-z_$][A-Za-z0-9_$]*', value)]
    roots = {'contents', 'generationconfig', 'systeminstruction', 'safetysettings', 'model'}
    if not tokens or len(tokens) > 24 or tokens[0] not in roots or not set(tokens) <= names:
        return None
    return value


def safe_error_diagnostic(response, approved_request, secret, model):
    """A quota metric is an observation, never proof of the account's billing tier."""
    result = dict(http_status=response.status_code, provider_status='unclassified',
                  reasons=[], field_paths=[], quota_metrics=[], automatic_retry=False,
                  account_tier_verified=False, raw_error_body_saved=False)
    response.read()
    if len(response.content) > 64000:
        return result
    try:
        packet = json.loads(response.content)
    except (ValueError, UnicodeDecodeError):
        return result
    error = packet.get('error') if isinstance(packet, dict) else None
    if not isinstance(error, dict):
        return result
    status = error.get('status')
    if isinstance(status, str) and status in STATUSES and not (secret and secret in status):
        result['provider_status'] = status
    # Some server rejections supply only a message, with no rpc.BadRequest.
    # Keep vocabulary tokens, never arbitrary prose, numbers, values or quotes.
    message=error.get('message')
    if isinstance(message,str):
        vocabulary={word for word in ('generatecontentrequest generationconfig responsejsonschema responseschema '
            'properties items required additionalproperties defs ref minitems maxitems schema invalid invalidargument '
            'payload unknown name field supported unsupported complex states many large exceed maximum minimum '
            'constraints type allowed only not cannot api key valid expired permission denied billing free tier limit quota requests').split()}
        def public_names(obj):
            if isinstance(obj,dict):
                for name,child in obj.items():
                    vocabulary.add(name.replace('_','').lstrip('$').casefold());public_names(child)
            elif isinstance(obj,list):
                for child in obj:public_names(child)
        public_names(approved_request)
        scrubbed=message.replace(secret,'') if secret else message
        terms=[term.replace('_','').lstrip('$').casefold() for term in re.findall(r'[A-Za-z_$][A-Za-z0-9_$]*',scrubbed)]
        terms=[term for term in terms if term in vocabulary][:64]
        if terms:result['message_terms']=terms
    details = error.get('details')
    if not isinstance(details, list):
        return result
    for detail in details[:32]:
        if not isinstance(detail, dict):
            continue
        kind = detail.get('@type')
        if kind == 'type.googleapis.com/google.rpc.ErrorInfo':
            reason = detail.get('reason')
            if isinstance(reason, str) and reason in REASONS and not (secret and secret in reason):
                result['reasons'].append(reason)
        elif kind == 'type.googleapis.com/google.rpc.BadRequest':
            violations = detail.get('fieldViolations')
            if not isinstance(violations, list):
                continue
            for violation in violations[:32]:
                if isinstance(violation, dict):
                    path = _field_path(violation.get('field'), approved_request, secret)
                    if path:
                        result['field_paths'].append(path)
        elif kind == 'type.googleapis.com/google.rpc.QuotaFailure':
            violations = detail.get('violations')
            if not isinstance(violations, list):
                continue
            for violation in violations[:32]:
                if not isinstance(violation, dict):
                    continue
                metric = violation.get('quotaMetric')
                if isinstance(metric, str) and metric in QUOTA_METRICS and not (secret and secret in metric):
                    result['quota_metrics'].append(metric)
                    dimensions = violation.get('quotaDimensions')
                    if isinstance(dimensions, dict) and dimensions.get('model') == model and not (secret and secret in model):
                        result['quota_model'] = model
        elif kind == 'type.googleapis.com/google.rpc.RetryInfo':
            delay = detail.get('retryDelay')
            if isinstance(delay, str) and re.fullmatch(r'\d{1,4}(?:\.\d{1,9})?s', delay):
                seconds = float(delay[:-1])
                if seconds <= 3600:
                    result['retry_delay_seconds'] = seconds
    for name in ('reasons', 'field_paths', 'quota_metrics'):
        result[name] = sorted(set(result[name]))[:32]
    return result
