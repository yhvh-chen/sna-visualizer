import requests
import geoip2.database
import json
from datetime import datetime, timedelta, timezone
import pandas as pd
import os
import folium
import ipaddress
import plotly.graph_objects as go
import plotly.offline as pyo

# --- Stealthwatch API Client ---

class StealthwatchClient:
    def __init__(self, host, username, password):
        self.host = host
        self.base_url = f"https://{host}"
        self.auth_url = f"{self.base_url}/token/v2/authenticate"
        self.api_session = requests.Session()
        # Allow controlling SSL verification via env var (default: False to preserve existing behavior)
        verify_ssl = os.getenv('SMC_VERIFY_SSL', 'false').lower() in ('1', 'true', 'yes')
        self.api_session.verify = verify_ssl
        requests.packages.urllib3.disable_warnings()
        self.tenant_id = None
        self.authenticate(username, password)

    def authenticate(self, username, password):
        try:
            login_data = {"username": username, "password": password}
            response = self.api_session.post(self.auth_url, data=login_data)
            response.raise_for_status()
            xsrf_token = response.cookies.get('XSRF-TOKEN')
            if not xsrf_token:
                raise ConnectionError("Authentication failed: XSRF-TOKEN not found in response cookies.")
            self.api_session.headers.update({'X-XSRF-TOKEN': xsrf_token})
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network error during authentication: {e}")
        except Exception as e:
            raise ConnectionError(f"Authentication error: {e}")

    def get_tenants(self):
        try:
            tenants_url = f'{self.base_url}/sw-reporting/v1/tenants/'
            response = self.api_session.get(tenants_url)
            response.raise_for_status()
            return response.json().get("data", [])
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network error fetching tenants: {e}")
        except Exception as e:
            raise ConnectionError(f"Error fetching tenants: {e}")

    def fetch_flows(self, tenant_id, start_time, end_time, subject_ips_in, subject_ips_ex,
                    peer_ips_in, peer_ips_ex, limit, progress=None):
        self.tenant_id = tenant_id
        flow_query_url = f"{self.base_url}/sw-reporting/v2/tenants/{self.tenant_id}/flows/queries"

        # Build the query payload
        query_payload = {
            "startDateTime": start_time,
            "endDateTime": end_time,
            "recordLimit": limit
        }
        if subject_ips_in:
            query_payload.setdefault("subject", {})["ipAddresses"] = {"includes": subject_ips_in}
        if subject_ips_ex:
            query_payload.setdefault("subject", {})["ipAddresses"] = {"excludes": subject_ips_ex}
        if peer_ips_in:
            query_payload.setdefault("peer", {})["ipAddresses"] = {"includes": peer_ips_in}
        if peer_ips_ex:
            query_payload.setdefault("peer", {})["ipAddresses"] = {"excludes": peer_ips_ex}

        headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
        # Use a configurable timeout for requests
        timeout = int(os.getenv('TIMEOUT_SECONDS', '30'))
        response = self.api_session.post(flow_query_url, data=json.dumps(query_payload), headers=headers, timeout=timeout)
        if response.status_code != 201:
            raise ValueError(f"API Error ({response.status_code}): {response.text}")

        query_id = response.json()["data"]["query"]["id"]
        status_url = f'{flow_query_url}/{query_id}'

        # Poll for results
        while True:
            status_response = self.api_session.get(status_url, timeout=timeout)
            status_response.raise_for_status()
            search_status = status_response.json()["data"]["query"]
            if progress:
                progress(search_status.get("percentComplete", 0) / 100, "Search in progress...")
            if search_status.get("percentComplete") == 100.0:
                break
            import time
            time.sleep(2)

        results_url = f'{status_url}/results'
        results_response = self.api_session.get(results_url, timeout=timeout)
        results_response.raise_for_status()
        return results_response.json().get("data", {}).get("flows", [])

# --- Data Processing ---

def process_flows_to_dataframe(flows):
    if not flows:
        return pd.DataFrame()
    
    processed_data = []
    for flow in flows:
        processed_data.append({
            "startTime": flow.get("statistics", {}).get("firstActiveTime"),
            "sourceIp": flow.get("subject", {}).get("ipAddress"),
            "sourcePort": flow.get("subject", {}).get("portProtocol", {}).get("port"),
            "destinationIp": flow.get("peer", {}).get("ipAddress"),
            "destinationPort": flow.get("peer", {}).get("portProtocol", {}).get("port"),
            "protocol": flow.get("protocol"),
            "totalBytes": flow.get("statistics", {}).get("byteCount")
        })
    return pd.DataFrame(processed_data)

# --- Geolocation ---

def init_geoip_database():
    # Respect GEOLITE_DB_PATH if provided, otherwise fall back to default
    db_path = os.getenv('GEOLITE_DB_PATH', 'GeoLite2-City.mmdb')
    if os.path.exists(db_path):
        return True

    # Decide whether to attempt automatic download
    download_enabled = os.getenv('DOWNLOAD_GEOLITE', 'false').lower() in ('1', 'true', 'yes')
    license_key = os.getenv('MAXMIND_LICENSE_KEY')
    if not download_enabled or not license_key:
        # Skip download if disabled or no license key available
        return False

    url = f"https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key={license_key}&suffix=tar.gz"
    timeout = int(os.getenv('TIMEOUT_SECONDS', '30'))
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code == 200:
            import tarfile
            import io
            with tarfile.open(fileobj=io.BytesIO(response.content), mode='r:gz') as tar:
                # Extract to a temp dir
                temp_dir = 'temp_geoip'
                os.makedirs(temp_dir, exist_ok=True)
                tar.extractall(temp_dir)
                # Find and move the .mmdb file
                found = False
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        if file.endswith('.mmdb'):
                            target_path = db_path
                            # Ensure parent dir exists
                            os.makedirs(os.path.dirname(target_path) or '.', exist_ok=True)
                            os.rename(os.path.join(root, file), target_path)
                            found = True
                            break
                    if found:
                        break
                # Clean up temp dir
                import shutil
                shutil.rmtree(temp_dir)
            if found:
                print(f"GeoLite DB saved to {db_path}")
                return True
            else:
                print("GeoLite DB not found inside downloaded archive.")
                return False
        else:
            print(f"Failed to download database: HTTP {response.status_code}")
            return False
    except Exception as e:
        print(f"Error downloading database: {e}")
        return False

def is_private_ip(ip):
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False

def get_geo_location(ip):
    if is_private_ip(ip):
        return None  # Skip private IPs
    if not init_geoip_database():
        return None  # Database not available
    try:
        db_path = os.getenv('GEOLITE_DB_PATH', 'GeoLite2-City.mmdb')
        with geoip2.database.Reader(db_path) as reader:
            response = reader.city(ip)
            return {
                "lat": response.location.latitude,
                "lon": response.location.longitude,
                "country": response.country.name,
                "city": response.city.name
            }
    except Exception as e:
        print(f"Error getting geo for {ip}: {e}")
    return None

# --- Visualization ---

def hex_to_rgba(hex_color, alpha=0.7):
    """Converts a HEX color string to an RGBA string."""
    hex_color = hex_color.lstrip('#')
    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    return f'rgba({r}, {g}, {b}, {alpha})'

def create_network_visualization(df, max_flows=10, html_page=True):
    """Creates an interactive Sankey diagram visualization for network traffic flows.
    
    This function processes network flow data and creates a Sankey diagram showing
    the top flows by data volume with an Elastiflow-inspired dark theme.
    
    Args:
        df: DataFrame with columns sourceIp, destinationIp, totalBytes, protocol
        max_flows: Number of top flows to visualize (default: 10)
    
    Returns:
        HTML string containing the Plotly Sankey diagram, or fallback HTML table
    """
    if df.empty:
        return None

    try:
        import plotly.express as px
        
        print(f"Creating Sankey diagram for top {max_flows} flows from {len(df)} total...")
        
        # 1. DATA PROCESSING AND FILTERING
        # Aggregate flows by source and destination IP, summing totalBytes
        flow_data_all = df.groupby(['sourceIp', 'destinationIp']).agg({
            'totalBytes': 'sum',
            'protocol': 'first'  # Take first protocol for the aggregated flow
        }).reset_index()
        
        # If max_flows is None or <= 0, show all aggregated flows
        if not max_flows or int(max_flows) <= 0:
            top_flows = flow_data_all.sort_values('totalBytes', ascending=False)
        else:
            # Filter for top N flows by traffic volume
            top_flows = flow_data_all.nlargest(int(max_flows), 'totalBytes')
        print(f"Selected {len(top_flows)} aggregated flows")
        
        if top_flows.empty:
            return create_simple_network_table(df, max_flows)
        
        # 2. DATA STRUCTURING FOR PLOTLY SANKEY
        # Create a list of unique nodes (all unique IPs)
        all_nodes = pd.unique(top_flows[['sourceIp', 'destinationIp']].values.ravel('K'))
        node_map = {node: i for i, node in enumerate(all_nodes)}
        
        print(f"Created {len(all_nodes)} unique nodes")
        
        # 3. STYLING AND THEMING
        # Generate unique, transparent colors for each flow
        color_palette = px.colors.qualitative.Plotly
        num_flows = len(top_flows)
        link_colors = [hex_to_rgba(color_palette[i % len(color_palette)], alpha=0.7) 
                      for i in range(num_flows)]
        
        # Prepare the 'link' dictionary with data and styling
        links = {
            'source': top_flows['sourceIp'].map(node_map).tolist(),
            'target': top_flows['destinationIp'].map(node_map).tolist(),
            'value': top_flows['totalBytes'].tolist(),
            'color': link_colors,
            'hovertemplate': (
                '<b>Flow:</b> %{source.label} → %{target.label}<br>'
                '<b>Total Bytes:</b> %{value:,.0f}<br>'
                '<b>Protocol:</b> ' + top_flows['protocol'].astype(str) + 
                '<extra></extra>'
            ).tolist()
        }
        
        # Add node colors with gradient based on traffic volume
        # Calculate total traffic per node
        node_traffic = {}
        for idx, row in top_flows.iterrows():
            src = row['sourceIp']
            dst = row['destinationIp']
            bytes_val = row['totalBytes']
            node_traffic[src] = node_traffic.get(src, 0) + bytes_val
            node_traffic[dst] = node_traffic.get(dst, 0) + bytes_val
        
        # Create node colors based on traffic volume
        max_traffic = max(node_traffic.values()) if node_traffic else 1
        node_colors = []
        for node in all_nodes:
            traffic = node_traffic.get(node, 0)
            # Scale from dark blue to bright blue based on traffic
            intensity = int(100 + (traffic / max_traffic) * 155)
            node_colors.append(f'rgba(100, {intensity}, 237, 0.8)')
        
        # 4. CHART GENERATION
        # Create the Sankey figure object
        fig = go.Figure(data=[go.Sankey(
            node=dict(
                pad=20,
                thickness=25,
                line=dict(color="rgba(255,255,255,0.3)", width=1),
                label=all_nodes.tolist(),
                color=node_colors,
                hovertemplate='<b>%{label}</b><br>Total Traffic: %{value:,.0f} bytes<extra></extra>'
            ),
            link=links,
            orientation='h',
            arrangement='snap'
        )])
        
        # Apply the final layout with Elastiflow-inspired dark theme
        fig.update_layout(
            title={
                'text': f"Top {max_flows} Network Flows - Traffic Analysis",
                'x': 0.5,
                'xanchor': 'center',
                'font': {'size': 18, 'color': 'rgba(255,255,255,0.9)'}
            },
            font=dict(
                size=12,
                family="'Segoe UI', 'Roboto', sans-serif",
                color='rgba(255,255,255,0.85)'
            ),
            template="plotly_dark",
            paper_bgcolor='rgba(15,15,25,1)',
            plot_bgcolor='rgba(15,15,25,1)',
            height=600,
            margin=dict(l=10, r=10, t=80, b=40),
            hoverlabel=dict(
                bgcolor="rgba(30,30,40,0.95)",
                font_size=13,
                font_family="'Segoe UI', sans-serif"
            )
        )

        # Convert to HTML
        plotly_div = pyo.plot(fig, output_type='div', include_plotlyjs='cdn')
        print("✅ Sankey diagram generated successfully")
        # Backwards-compatible behavior: return full HTML page by default, or
        # return the raw Plotly div if html_page is False (for embedding).
        if not html_page:
            return plotly_div

        # Minimal HTML wrapper (dark-themed to match Plotly dark template)
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Network Traffic Sankey Diagram</title>
    <style>
        body {{ background: #0f0f19; color: #fff; font-family: 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ text-align: center; margin-bottom: 12px; }}
    </style>
</head>
<body>
    <div class="container"> 
        <div class="header"> 
            <h1 style="margin:0; padding:0;">Top {max_flows} Network Flows - Traffic Analysis</h1>
            <p style="opacity:0.8; margin-top:6px;">Interactive Sankey diagram (Plotly)</p>
        </div>
        {plotly_div}
    </div>
</body>
</html>"""
        return html_content
        
    except ImportError as e:
        print(f"❌ Missing library: {e}. Falling back to simple table")
        return create_simple_network_table(df, max_flows)
    except Exception as e:
        print(f"❌ Sankey diagram error: {e}")
        import traceback
        traceback.print_exc()
        return create_simple_network_table(df, max_flows)

def create_simple_network_table(df, max_flows=10):
    """Fallback: create a simple HTML table showing top flows"""
    if df.empty:
        return "<p>No data to display</p>"
    
    top_flows = df.nlargest(max_flows, 'totalBytes')
    
    html = f"""
    <div style="background: white; padding: 20px; border-radius: 5px;">
        <h3>Top {max_flows} Network Flows by Traffic Volume</h3>
        <table style="width: 100%; border-collapse: collapse; font-family: Arial, sans-serif;">
            <thead>
                <tr style="background-color: #f0f0f0;">
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Source IP</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Destination IP</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Protocol</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: right;">Bytes</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Traffic Bar</th>
                </tr>
            </thead>
            <tbody>
    """
    
    max_bytes = top_flows['totalBytes'].max()
    for index, row in top_flows.iterrows():
        bytes_val = int(row['totalBytes'])
        percentage = (bytes_val / max_bytes) * 100 if max_bytes > 0 else 0
        
        html += f"""
                <tr>
                    <td style="border: 1px solid #ddd; padding: 8px;">{row['sourceIp']}</td>
                    <td style="border: 1px solid #ddd; padding: 8px;">{row['destinationIp']}</td>
                    <td style="border: 1px solid #ddd; padding: 8px;">{row['protocol']}</td>
                    <td style="border: 1px solid #ddd; padding: 8px; text-align: right;">{bytes_val:,}</td>
                    <td style="border: 1px solid #ddd; padding: 8px;">
                        <div style="background-color: #e0e0e0; width: 200px; height: 20px; border-radius: 10px;">
                            <div style="background-color: #4CAF50; width: {percentage}%; height: 100%; border-radius: 10px;"></div>
                        </div>
                    </td>
                </tr>
        """
    
    html += """
            </tbody>
        </table>
    </div>
    """
    
    return html


def create_pie_chart(df, column, top_n=10, by_sum=False, title=None, template="plotly_dark"):
    """Create a Plotly pie chart HTML fragment for a DataFrame.

    Args:
        df: pandas DataFrame
        column: column name to aggregate by (e.g., 'protocol')
        top_n: number of top slices to keep (others aggregated to 'Other')
        by_sum: if True, size slices by sum of totalBytes, else by count
        title: optional chart title
        template: plotly template

    Returns:
        HTML string (div) with the chart (Plotly CDN included by default when embedding page)
    """
    try:
        import plotly.express as px
        import plotly.io as pio
    except ImportError:
        return "<div>No plotting library available</div>"

    if df is None or df.empty:
        return "<div>No data to display</div>"

    if by_sum and 'totalBytes' in df.columns:
        agg = df.groupby(column)['totalBytes'].sum().reset_index(name='value')
    else:
        agg = df.groupby(column).size().reset_index(name='value')

    agg = agg.sort_values('value', ascending=False)
    if top_n and len(agg) > top_n:
        top = agg.head(top_n)
        other = pd.DataFrame({column: ['Other'], 'value': [agg['value'].iloc[top_n:].sum()]})
        agg = pd.concat([top, other], ignore_index=True)

    fig = px.pie(agg, names=column, values='value', title=(title or f'Top {top_n} by {column}'), template=template)
    fig.update_traces(textinfo='percent+label', hovertemplate='%{label}<br><b>%{value:,.0f}</b><extra></extra>')
    return pio.to_html(fig, full_html=False, include_plotlyjs='cdn')

def create_world_map(df):
    if df is None or df.empty:
        return None

    # Ensure columns exist
    if 'sourceIp' not in df.columns or 'destinationIp' not in df.columns:
        return None

    # Create a map centered on the world
    m = folium.Map(location=[20, 0], zoom_start=2)

    # Collect unique IPs and their geo
    ip_geo = {}
    for ip in pd.concat([df['sourceIp'], df['destinationIp']]).unique():
        if not isinstance(ip, str) or not ip:
            continue
        if ip not in ip_geo:
            try:
                geo = get_geo_location(ip)
            except Exception:
                geo = None
            if geo and geo.get('lat') is not None and geo.get('lon') is not None:
                try:
                    lat = float(geo.get('lat'))
                    lon = float(geo.get('lon'))
                except (TypeError, ValueError):
                    continue
                ip_geo[ip] = {'lat': lat, 'lon': lon, 'city': geo.get('city', ''), 'country': geo.get('country', '')}

    # Add markers (skip invalid coords)
    for ip, geo in ip_geo.items():
        lat = geo.get('lat')
        lon = geo.get('lon')
        if lat is None or lon is None:
            continue
        folium.Marker(
            location=[lat, lon],
            popup=f"{ip}<br>{geo.get('city', '')}, {geo.get('country', '')}",
            tooltip=ip
        ).add_to(m)

    # Add connection lines between source and destination IPs that both have geo locations
    for idx, row in df.iterrows():
        src_ip = row.get('sourceIp')
        dst_ip = row.get('destinationIp')
        
        if src_ip in ip_geo and dst_ip in ip_geo:
            src_geo = ip_geo[src_ip]
            dst_geo = ip_geo[dst_ip]
            
            # Draw a line between the two points
            folium.PolyLine(
                locations=[
                    [src_geo['lat'], src_geo['lon']],
                    [dst_geo['lat'], dst_geo['lon']]
                ],
                color='blue',
                weight=2,
                opacity=0.6,
                popup=f"Connection: {src_ip} → {dst_ip}<br>Bytes: {row.get('totalBytes', 'N/A')}"
            ).add_to(m)

    # Return HTML string
    return m._repr_html_()