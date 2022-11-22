#!/usr/bin/env python3
import argparse
import os
import re
import signalfx
import string
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description='Export a Splunk Observability Cloud asset as Terraform')
parser.add_argument('--key', dest='key', required=True, help='An API key for accessing Splunk Observability Cloud')
parser.add_argument('--api_url', dest='api_url', help='The API URL, used for non-us1 realms', default='https://api.us1.signalfx.com')
parser.add_argument('--name', dest='name', required=True, help='The name of the resource after export, e.g. mychart0')
parser.add_argument('--exclude', dest='excludes', nargs='*', help='A field to exclude from the emitted HCL', default=['id', 'url', 'tags'])
parser.add_argument('--output', dest='output', required=True, help='The name of the directory to which output will be written')
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument('--group', dest='group', help='The ID of the dashboard group in Splunk Observability Cloud')
group.add_argument('--dashboard', dest='dash', help='The ID of the dashboard in Splunk Observability Cloud')
group.add_argument('--detector', dest='detector', help='The ID of the detector in Splunk Observability Cloud')
group.add_argument('--globaldatalink', dest='globaldatalink', help='The colon : seperated property name and value for a global data link - if value is wildcarded just provide the name')

args = vars(parser.parse_args())

def init_terraform(tdir, key, api_url, resource, name):
    provider = '''
terraform {
  required_providers {
    signalfx = {
      source = "splunk-terraform/signalfx"
    }
  }
  required_version = ">= 1.0"
}
'''
    with open('main.tf', 'w') as tffile:
        tffile.write('provider "signalfx" {\n')
        tffile.write(f'\tauth_token = "{key}"\n')
        tffile.write(f'\tapi_url = "{api_url}"\n')
        tffile.write('}\n')
        tffile.write(f'resource "{resource}" "{name}" {{\n')
        tffile.write('}\n')
        tffile.write(provider)
    subprocess.check_output(['terraform', 'init'])

def import_resource(tdir, resource, name, id):
    subprocess.check_output(['terraform', 'import', f'{resource}.{name}', id])

def export_resource(tdir, resource, name):
    output = subprocess.check_output(['terraform', 'state', 'show', '-no-color', f'{resource}.{name}'])
    return output

def handle_asset(key, api_url, resource, name, id):
    currdir = os.getcwd()
    output = None
    with tempfile.TemporaryDirectory() as tmpdirname:
        os.chdir(tmpdirname)
        init_terraform(tmpdirname, key, api_url, resource, name)
        import_resource(tmpdirname, resource, name, id)
        output = export_resource(tmpdirname, resource, name)
    os.chdir(currdir)
    return output

def write_output(directory, filename, content):
    currdir = os.getcwd()

    os.chdir(directory)
    with open(filename, "w") as f:
        f.write(filter_hcl(content))

    os.chdir(currdir)

# Handles replacing various problematic bits of output. Some are computed fields, others are problematic parts of an export.
def filter_hcl(hcl):

    # Look for a bare label in viz_options, which seems to break stuff.
    hcl = re.sub('viz_options\s*\{\n\s*label\s*=\s\".*\"\n\s*\s}\n', '', hcl)
    # Replace the EOT thing that doesn't work with an EOF
    hcl = re.sub('<<~EOT', '<<-EOF', hcl)
    hcl = re.sub('EOT', 'EOF', hcl)

    keep = list()
    if len(args['excludes']) < 1:
        return hcl
    for line in hcl.splitlines():
        exclude = False
        for ex in args['excludes']:
            regex = f'^\s*{ex}\s*='
            match = re.search(regex, line)
            if match:
                exclude = True
                break
            # This catches the silly fixed "max" fields that come out of the
            # import but are not meant to be in the config itself. - Cory
            if "17976931348623157" in line:
                exclude = True
                break
        if not exclude:
            keep.append(line)
    return "\n".join(keep)

def replace_id_with_name(hcl, id, name):
    hcl = hcl.replace(f'"{id}"', f'{name}.id')
    return hcl

def replace_chart_ids(hcl, charts):
    for id, name in charts.items():
        hcl = replace_id_with_name(hcl, id, name)
    return hcl

def handle_detector(sfx, id, name, args):
    det = sfx.get_detector(id)

    return handle_asset(args['key'], args['api_url'], "signalfx_detector", name, id)

def handle_datalink(sfx, props, name, args):
    propertyName=props.split(":")[0]
    propertyValue=""
    if len(props.split(":")) > 1:
        propertyValue=props.split(":")[1]
    dets = sfx.get_datalinks(None,propertyName,propertyValue)

    if dets["count"] < 1:
        print('There isnt a globaldatalink matching those values')
        sys.exit(1)
    return handle_asset(args['key'], args['api_url'], "signalfx_data_link", name, dets["results"][0]["id"])

def handle_dashboard(sfx, id, name, args):
    dash = sfx.get_dashboard(id)

    # I am not certain of the API guarantees that these will come in a sane
    # order so we'll just sort it for human readability.
    charts = sorted(dash["charts"], key=lambda k: (k["row"], k["column"]))

    chart_types = []
    chart_ids = {}
    out = ""
    # Iterate through and fetch each chart
    for i, chart in enumerate(charts):
        api_chart = sfx.get_chart(chart["chartId"])
        chart_type = api_chart["options"]["type"]
        tf_type = "signalfx_time_chart"
        if chart_type == "SingleValue":
            tf_type = "signalfx_single_value_chart"
        elif chart_type == "List":
            tf_type = "signalfx_list_chart"
        elif chart_type == "Text":
            tf_type = "signalfx_text_chart"
        elif chart_type == "TimeSeriesChart":
            tf_type = "signalfx_time_chart"
        elif chart_type == "Heatmap":
            tf_type = "signalfx_heatmap_chart"
        elif chart_type == "Event":
            tf_type = "signalfx_event_feed_chart"
        else:
            print(f"Exiting: Unknown chart type {chart_type}", file=sys.stderr)
            sys.exit()

        chart_types.append(tf_type)
        tf_name = f"{name}_{i}"
        chart_ids[chart['chartId']] = f"{tf_type}.{tf_name}"

        output = handle_asset(args['key'], args['api_url'], tf_type, tf_name, chart['chartId'])
        if output != None:
            out += filter_hcl(output.decode('utf-8'))
            out += "\n"

    output = handle_asset(args['key'], args['api_url'], "signalfx_dashboard", name, id)
    if output != None:
         out += replace_chart_ids(filter_hcl(output.decode('utf-8')), chart_ids)

    return out

with signalfx.SignalFx(
    api_endpoint=args['api_url'],
).rest(args['key']) as sfx:
    if args['group']:
        group = sfx.get_dashboard_group(args['group'])
        for i, dash in enumerate(group['dashboards']):
            print(f"Exporting dashboard {dash}")
            dash_name = args['name'] + f"_dash_{i}"
            dash_out = handle_dashboard(sfx, dash, dash_name, args)
            # Replace the dashboard group id
            dash_out = replace_id_with_name(dash_out, args['group'], f"signalfx_dashboard_group.{args['name']}")
            write_output(args['output'], dash_name + ".tf", dash_out)

        output = handle_asset(args['key'], args['api_url'], "signalfx_dashboard_group", args['name'], args['group'])
        if output != None:
            write_output(args['output'], args['name'] + ".tf", filter_hcl(output.decode('utf-8')))
    elif args['dash']:
        dash_out = handle_dashboard(sfx, args['dash'], args['name'], args)
        write_output(args['output'], args['name'] + ".tf", dash_out)
    elif args['detector']:
        det_out = handle_detector(sfx, args['detector'], args['name'], args)
        write_output(args['output'], args['name'] + ".tf", det_out.decode('utf-8'))
    elif args['globaldatalink']:
        det_out = handle_datalink(sfx, args['globaldatalink'], args['name'], args)
        write_output(args['output'], args['name'] + ".tf", det_out.decode('utf-8'))
    else:
        print('Use one of --dashboard, --detector, --globaldatalink or --group')
        sys.exit(1)
