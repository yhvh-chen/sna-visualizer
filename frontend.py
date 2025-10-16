import gradio as gr
import pandas as pd
from datetime import datetime, timedelta, timezone
from backend import StealthwatchClient, process_flows_to_dataframe, create_network_visualization, create_world_map, create_pie_chart
import os

# --- Gradio Interface Logic ---

def connect_to_smc(host, username, password, current_log):
    try:
        client = StealthwatchClient(host, username, password)
        tenants = client.get_tenants()
        tenant_names = [f"{t.get('name', 'Unknown')} (ID: {t.get('id', 'N/A')})" for t in tenants]
        
        success_msg = f"""
✅ Successfully connected to SMC at {host}!
👤 Logged in as: {username}
🏢 Found {len(tenants)} tenant(s)
📋 Please select a tenant from the dropdown above to continue.
"""
        updated_log = current_log + "\n" + success_msg
        
        return {
            smc_client: client,
            tenant_dd: gr.Dropdown(choices=tenant_names, label="Select Tenant", visible=True),
            connect_btn: gr.Button(value="✅ Connected", interactive=False),
            filters_interface: gr.Column(visible=True),
            viz_interface: gr.Column(visible=True),
            log_box: updated_log,
            log_state: updated_log
        }
    except Exception as e:
        error_msg = f"❌ Connection failed: {e}"
        updated_log = current_log + "\n" + error_msg
        gr.Warning(f"Connection Failed: {e}")
        return {
            smc_client: None,
            tenant_dd: gr.Dropdown(visible=False),
            connect_btn: gr.Button(value="Connect", interactive=True),
            filters_interface: gr.Column(visible=False),
            viz_interface: gr.Column(visible=False),
            log_box: updated_log,
            log_state: updated_log
        }

def get_time_range(selection):
    now_utc = datetime.now(timezone.utc)
    if selection == "Custom":
        return gr.Group(visible=True)
    
    delta_map = {
        "Last 5 Minutes": timedelta(minutes=5),
        "Last 1 Hour": timedelta(hours=1),
        "Last 12 Hours": timedelta(hours=12),
        "Last 24 Hours": timedelta(days=1),
        "Last 7 Days": timedelta(days=7),
    }
    delta = delta_map.get(selection)
    if delta:
        start_time = now_utc - delta
        return start_time.strftime('%Y-%m-%dT%H:%M:%SZ'), now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'), gr.Group(visible=False)
    return None, None, gr.Group(visible=False)

def run_search(client, tenant_str, time_range_sel, start_time_custom, end_time_custom,
               sub_in, sub_ex, peer_in, peer_ex, limit, current_log,
               progress=gr.Progress()):
    if not client:
        error_msg = "❌ Please connect to the SMC first."
        gr.Warning("Please connect to the SMC first.")
        return current_log + "\n" + error_msg, "<p>No files generated yet</p>", None, current_log + "\n" + error_msg

    try:
        # Determine tenant ID
        if not tenant_str:
            error_msg = "❌ Please select a tenant from the dropdown."
            gr.Warning("Please select a tenant from the dropdown.")
            return current_log + "\n" + error_msg, "<p>No files generated yet</p>", None, current_log + "\n" + error_msg
        
        tenant_id = tenant_str.split(" (ID: ")[1][:-1]
        
        # Determine time range
        if time_range_sel != "Custom":
            start_time, end_time, _ = get_time_range(time_range_sel)
        else:
            start_time = datetime.fromisoformat(start_time_custom.replace("Z", "+00:00")).strftime('%Y-%m-%dT%H:%M:%SZ')
            end_time = datetime.fromisoformat(end_time_custom.replace("Z", "+00:00")).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Parse IPs from comma-separated strings
        parse = lambda s: [ip.strip() for ip in s.split(',') if ip.strip()]
        
        # Log search parameters
        search_log = f"""
🔍 Starting search with parameters:
  Tenant ID: {tenant_id}
  Time Range: {start_time} to {end_time}
  Source Include: {parse(sub_in) or 'All'}
  Source Exclude: {parse(sub_ex) or 'None'}
  Dest Include: {parse(peer_in) or 'All'}
  Dest Exclude: {parse(peer_ex) or 'None'}
  Record Limit: {limit}
"""
        updated_log = current_log + "\n" + search_log
        
        flows = client.fetch_flows(
            tenant_id, start_time, end_time,
            parse(sub_in), parse(sub_ex),
            parse(peer_in), parse(peer_ex),
            limit, progress
        )
        
        flow_count = len(flows) if flows else 0
        flow_log = f"📊 API returned {flow_count} flows"
        updated_log = updated_log + "\n" + flow_log
        
        if not flows:
            no_data_msg = f"""
⚠️ No data found for the given criteria.
Suggestions:
• Try expanding the time range
• Remove or adjust IP filters  
• Check if the tenant has active traffic
• Verify network connectivity to SMC
"""
            gr.Info("Search complete. No flows found for the given criteria.")
            return updated_log + "\n" + no_data_msg, "<p>No files generated yet</p>", None

        df = process_flows_to_dataframe(flows)
        
        # Create output directory if it doesn't exist
        import os
        os.makedirs('output', exist_ok=True)
        
        # Save dataframe to CSV in output folder
        csv_path = os.path.abspath("output/traffic_dump.csv")
        df.to_csv(csv_path, index=False)
        
        success_log = f"""
✅ Search completed successfully!
📄 Data saved to: {csv_path}
📊 Found {len(df)} flows, {len(df['sourceIp'].unique())} unique sources, {len(df['destinationIp'].unique())} unique destinations
"""

        # Create output files HTML
        csv_link = f"file:///{csv_path.replace(chr(92), '/')}"
        output_files_html = f"""
<p><strong>Generated Files:</strong></p>
<p>📄 <a href="{csv_link}" target="_blank">traffic_dump.csv</a> ({len(df)} flows)</p>
<p><em>Run visualization generation to create HTML report</em></p>
"""
        
        gr.Info("Search complete. Data saved successfully.")
        return updated_log + "\n" + success_log, output_files_html, csv_path, updated_log + "\n" + success_log

    except Exception as e:
        error_log = f"❌ Error during search: {e}"
        gr.Error(f"An error occurred: {e}")
        return current_log + "\n" + error_log, "<p>No files generated yet</p>", None, current_log + "\n" + error_log

def generate_visualizations(csv_path, top_n, current_log):
    if not csv_path:
        error_msg = "❌ No data available. Please run a search first."
        return current_log + "\n" + error_msg, "<p>No files generated yet</p>", current_log + "\n" + error_msg
    
    try:
        df = pd.read_csv(csv_path)
        
        viz_log = f"""
🎨 Generating visualizations for {len(df)} flows (Top {top_n if top_n != 0 else 'All'})...
"""
        updated_log = current_log + "\n" + viz_log
        
        # Create embedded fragments (Sankey div, folium map, pie divs)
        # top_n == 0 -> show all, otherwise show top_n
        max_flows_val = int(top_n) if top_n is not None else 10
        sankey_div = create_network_visualization(df, max_flows=(0 if max_flows_val == 0 else max_flows_val), html_page=False)

        # World map fragment
        map_fragment = create_world_map(df) or "<p>No geographic data available</p>"

        # Use top_n for pies as well (0 -> all)
        pn = int(top_n) if top_n is not None else 10
        prot_div = create_pie_chart(df, 'protocol', top_n=pn, by_sum=True, title='Protocols by Bytes')
        dest_div = create_pie_chart(df, 'destinationIp', top_n=pn, by_sum=True, title='Top Destination IPs by Bytes')
        src_div = create_pie_chart(df, 'sourceIp', top_n=pn, by_sum=True, title='Top Source IPs by Bytes')

        # Build a single combined HTML page embedding all visual fragments
        combined_path = os.path.abspath('output/combined_visualizations.html')
        # Include Plotly CDN once and some CSS layout
        combined_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Secure Network Analytics Visualizations</title>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body {{ background: #0f0f19; color: #fff; font-family: 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ text-align: center; margin-bottom: 18px; }}
        .grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }}
        .full {{ grid-column: 1 / -1; }}
        .card {{ background: rgba(255,255,255,0.04); padding: 12px; border-radius: 10px; box-shadow: 0 6px 18px rgba(0,0,0,0.4); }}
        .pies {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
        iframe, .plotly, .folium-map {{ width: 100%; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 style="margin:0;">Combined Network Visualizations</h1>
            <p style="opacity:0.8; margin-top:6px;">Top N = {pn if pn else 'All'}</p>
        </div>

        <div class="pies-row">
            <div class="card pies">
                <div>
                    <h3 style="margin-top:0;">Protocols</h3>
                    {prot_div}
                </div>
                <div>
                    <h3 style="margin-top:0;">Top Destinations</h3>
                    {dest_div}
                </div>
                <div>
                    <h3 style="margin-top:0;">Top Sources</h3>
                    {src_div}
                </div>
            </div>
        </div>

        <div class="card full">
            <h2 style="margin-top:0;">Traffic Flows</h2>
            {sankey_div or '<p>No sankey available</p>'}
        </div>

        <div class="card full">
            <h2 style="margin-top:0;">World Map</h2>
            {map_fragment}
        </div>

        <div style="margin-top:18px;" class="card">
            <strong>Data:</strong> {len(df)} flows, {len(df['sourceIp'].unique())} unique sources, {len(df['destinationIp'].unique())} unique destinations
            <div style="margin-top:8px; font-family: monospace; font-size: 13px;">CSV: {csv_path}</div>
        </div>
    </div>
</body>
</html>"""

        with open(combined_path, 'w', encoding='utf-8') as f:
            f.write(combined_html)

        # Create output files HTML with both CSV and HTML links
        csv_link = f"file:///{csv_path.replace(chr(92), '/')}"
        html_link = f"file:///{combined_path.replace(chr(92), '/')}"
        output_files_html = f"""
<p><strong>Generated Files:</strong></p>
<p>📄 <a href="{csv_link}" target="_blank">traffic_dump.csv</a> ({len(df)} flows)</p>
<p>📊 <a href="{html_link}" target="_blank">combined_visualizations.html</a> (Interactive Report)</p>
"""
        
        success_log = f"""
✅ Visualizations generated successfully!
📊 HTML report saved to: {combined_path}
🎯 Created interactive charts: Sankey diagram, world map, and pie charts
"""
        
        gr.Info("Visualization generation completed.")
        return updated_log + "\n" + success_log, output_files_html, updated_log + "\n" + success_log

    except Exception as e:
        error_log = f"❌ Error generating visualizations: {e}"
        return current_log + "\n" + error_log, "<p>No files generated yet</p>", current_log + "\n" + error_log

# --- Build Gradio UI ---

with gr.Blocks(theme=gr.themes.Soft(), title="SNA Visualizer") as demo:
    smc_client = gr.State(None)
    csv_path_state = gr.State(None)  # Shared state for CSV path
    log_state = gr.State("")  # State for accumulating log messages

    gr.Markdown("# SNA Visualizer")
    gr.Markdown("A GUI to query and visualize flow data from Cisco Secure Network Analytics (Stealthwatch).")

    # Main layout: Left column for connection, right column for tabs
    with gr.Row():
        # Left column: Connection (always visible, smaller size)
        with gr.Column(scale=1):
            gr.Markdown("### 🔗 Connection")
            # gr.Markdown("Connect to the SMC server.")
            smc_host = gr.Textbox(label="SMC Host/IP", value=os.getenv('SMC_HOSTNAME', ''))
            smc_user = gr.Textbox(label="Username", value=os.getenv('SMC_USERNAME', ''))
            smc_pass = gr.Textbox(label="Password", type="password", value=os.getenv('SMC_PASSWORD', ''))
            connect_btn = gr.Button("Connect")
            tenant_dd = gr.Dropdown(label="Select Tenant", visible=False)

        # Right column: Tabs for API Filters and Visualization Customizer
        with gr.Column(scale=3):
            with gr.Tabs():
                with gr.TabItem("🔍 API Filters"):
                    # gr.Markdown("Configure search filters and fetch data.")
                    with gr.Column(visible=False) as filters_interface:
                        with gr.Row():
                            time_range_dd = gr.Dropdown(
                                ["Last 5 Minutes", "Last 1 Hour", "Last 12 Hours", "Last 24 Hours", "Last 7 Days", "Custom"],
                                label="Time Range", value="Last 1 Hour")
                            with gr.Group(visible=False) as custom_time_group:
                                start_time_tb = gr.Textbox(label="Start Time (YYYY-MM-DDTHH:MM:SSZ)")
                                end_time_tb = gr.Textbox(label="End Time (YYYY-MM-DDTHH:MM:SSZ)")

                        with gr.Accordion("IP Filters", open=True):
                            with gr.Row():
                                subject_includes = gr.Textbox(label="Source IPs to Include (comma-separated)")
                                subject_excludes = gr.Textbox(label="Source IPs to Exclude (comma-separated)")
                            with gr.Row():
                                peer_includes = gr.Textbox(label="Destination IPs to Include (comma-separated)")
                                peer_excludes = gr.Textbox(label="Destination IPs to Exclude (comma-separated)")

                        limit_slider = gr.Slider(10, 10000, value=1000, step=10, label="Record Limit")
                        run_search_btn = gr.Button("Run Search", variant="primary")

                with gr.TabItem("📊 Visualization Customizer"):
                    # gr.Markdown("Customize and generate visualizations from fetched data.")
                    with gr.Column(visible=False) as viz_interface:
                        top_n_slider = gr.Slider(0, 1000, value=10, step=1, label="Top N for charts (0 = All)")
                        generate_viz_btn = gr.Button("Generate Visualizations", variant="primary")

    # Bottom section: Output
    gr.Markdown("---")
    # gr.Markdown("### Activity Log & Output Files")
    with gr.Row():
        with gr.Column(scale=3):
            log_box = gr.Textbox(
                label="Activity Log",
                lines=10,
                interactive=False,
                value="Welcome to SNA Traffic Data Dump Tool\nReady to connect and analyze network traffic...\n"
            )
        with gr.Column(scale=1):
            gr.Markdown("**Output Files**")
            output_files_box = gr.HTML(value="<p>No files generated yet</p>")

    # --- Component Interactions ---
    connect_btn.click(
        connect_to_smc,
        inputs=[smc_host, smc_user, smc_pass, log_state],
        outputs=[smc_client, tenant_dd, connect_btn, filters_interface, viz_interface, log_box, log_state]
    )

    time_range_dd.change(
        lambda sel: gr.Group(visible=(sel == "Custom")),
        inputs=time_range_dd,
        outputs=custom_time_group
    )

    run_search_btn.click(
        run_search,
        inputs=[smc_client, tenant_dd, time_range_dd, start_time_tb, end_time_tb,
                subject_includes, subject_excludes, peer_includes, peer_excludes, limit_slider, log_state],
        outputs=[log_box, output_files_box, csv_path_state, log_state]
    )

    generate_viz_btn.click(
        generate_visualizations,
        inputs=[csv_path_state, top_n_slider, log_state],
        outputs=[log_box, output_files_box, log_state]
    )