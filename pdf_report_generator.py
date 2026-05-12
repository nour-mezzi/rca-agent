#!/usr/bin/env python3
"""
PDF Report Generator for RCA Analysis
Converts JSON RCA output to a professional PDF report
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
from reportlab.lib import colors
from reportlab.pdfgen import canvas


class RCAPDFReport:
    def __init__(self, rca_json_path: str, output_pdf_path: str = None):
        """Initialize PDF report generator"""
        self.rca_path = Path(rca_json_path)
        self.rca_data = self._load_json()
        
        if output_pdf_path is None:
            output_pdf_path = self.rca_path.parent / f"RCA_Report_{self.rca_data['anomaly_id']}.pdf"
        
        self.output_path = Path(output_pdf_path)
        
        # Setup page
        self.pagesize = A4
        self.doc = SimpleDocTemplate(
            str(self.output_path),
            pagesize=self.pagesize,
            rightMargin=0.75*inch,
            leftMargin=0.75*inch,
            topMargin=0.75*inch,
            bottomMargin=0.75*inch,
        )
        
        # Story for document
        self.story = []
        
        # Setup styles
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()
    
    def _load_json(self) -> dict:
        """Load RCA JSON file"""
        try:
            return json.loads(self.rca_path.read_text())
        except Exception as e:
            raise ValueError(f"Failed to load RCA JSON: {e}")
    
    def _setup_custom_styles(self):
        """Setup custom paragraph styles"""
        # Title style
        self.styles.add(ParagraphStyle(
            name='CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1F4788'),
            spaceAfter=6,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold',
        ))
        
        # Subtitle style
        self.styles.add(ParagraphStyle(
            name='CustomSubtitle',
            parent=self.styles['Heading2'],
            fontSize=12,
            textColor=colors.HexColor('#555555'),
            spaceAfter=12,
            alignment=TA_CENTER,
            fontName='Helvetica',
        ))
        
        # Heading styles
        self.styles.add(ParagraphStyle(
            name='SectionHeading',
            parent=self.styles['Heading2'],
            fontSize=14,
            textColor=colors.HexColor('#1F4788'),
            spaceAfter=8,
            spaceBefore=12,
            fontName='Helvetica-Bold',
        ))
        
        # Body text
        self.styles.add(ParagraphStyle(
            name='BodyText',
            parent=self.styles['BodyText'],
            fontSize=10,
            alignment=TA_JUSTIFY,
            spaceAfter=6,
        ))
        
        # Code/Evidence style
        self.styles.add(ParagraphStyle(
            name='Evidence',
            parent=self.styles['Normal'],
            fontSize=9,
            textColor=colors.HexColor('#333333'),
            leftIndent=0.2*inch,
            spaceAfter=4,
            fontName='Courier',
        ))
    
    def _add_title_page(self):
        """Add title page"""
        self.story.append(Spacer(1, 1.5*inch))
        
        title = Paragraph("ROOT CAUSE ANALYSIS REPORT", self.styles['CustomTitle'])
        self.story.append(title)
        
        self.story.append(Spacer(1, 0.3*inch))
        
        anomaly_id = self.rca_data.get('anomaly_id', 'Unknown')
        subtitle = Paragraph(f"Anomaly ID: {anomaly_id}", self.styles['CustomSubtitle'])
        self.story.append(subtitle)
        
        self.story.append(Spacer(1, 1.5*inch))
        
        # Window info
        window = self.rca_data.get('window_utc', {})
        start = window.get('start', 'N/A')
        end = window.get('end', 'N/A')
        
        window_text = f"""
        <b>Analysis Window (UTC):</b><br/>
        Start: {start}<br/>
        End: {end}
        """
        self.story.append(Paragraph(window_text, self.styles['Normal']))
        
        self.story.append(Spacer(1, 2*inch))
        
        # Footer
        report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        footer_text = f"<i>Report Generated: {report_date}</i>"
        self.story.append(Paragraph(footer_text, self.styles['Normal']))
        
        self.story.append(PageBreak())
    
    def _add_executive_summary(self):
        """Add executive summary"""
        self.story.append(Paragraph("Executive Summary", self.styles['SectionHeading']))
        
        root_cause = self.rca_data.get('root_cause', {})
        summary = root_cause.get('summary', 'No summary available')
        confidence = root_cause.get('confidence', 'Unknown')
        
        # Confidence badge
        confidence_color = '#27AE60' if confidence == 'Confirmed' else '#F39C12'
        confidence_text = f"<b>Confidence Level:</b> <font color='{confidence_color}'><b>{confidence}</b></font>"
        self.story.append(Paragraph(confidence_text, self.styles['Normal']))
        
        self.story.append(Spacer(1, 0.15*inch))
        
        # Summary
        self.story.append(Paragraph(summary, self.styles['BodyText']))
        
        self.story.append(Spacer(1, 0.2*inch))
    
    def _add_affected_services(self):
        """Add affected services section with table"""
        self.story.append(Paragraph("Affected Services", self.styles['SectionHeading']))
        
        services = self.rca_data.get('affected_services', [])
        
        # Build table data
        table_data = [['Service', 'Anomaly Type', 'Details', 'Peak Time (UTC)']]
        
        for svc in services:
            service_name = svc.get('service', 'Unknown')
            anomaly_type = svc.get('anomaly_type', 'N/A').replace('_', ' ').title()
            details = svc.get('details', 'No details')
            peak_time = svc.get('peak_time_utc', 'N/A')
            
            # Truncate details if too long
            if len(details) > 100:
                details = details[:100] + "..."
            
            table_data.append([service_name, anomaly_type, details, peak_time or 'N/A'])
        
        # Create table
        table = Table(table_data, colWidths=[1.2*inch, 1.2*inch, 2.5*inch, 1.1*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4788')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F5F5')]),
        ]))
        
        self.story.append(table)
        self.story.append(Spacer(1, 0.2*inch))
    
    def _add_root_cause_analysis(self):
        """Add root cause analysis section"""
        self.story.append(Paragraph("Root Cause Analysis", self.styles['SectionHeading']))
        
        root_cause = self.rca_data.get('root_cause', {})
        
        # Primary service
        primary_service = root_cause.get('primary_service', 'Unknown')
        self.story.append(Paragraph(
            f"<b>Primary Service:</b> {primary_service}",
            self.styles['Normal']
        ))
        
        # Failure mode
        failure_mode = root_cause.get('failure_mode', 'Unknown').replace('_', ' ').title()
        self.story.append(Paragraph(
            f"<b>Failure Mode:</b> {failure_mode}",
            self.styles['Normal']
        ))
        
        self.story.append(Spacer(1, 0.1*inch))
        
        # Description
        description = root_cause.get('description', 'No description available')
        self.story.append(Paragraph(description, self.styles['BodyText']))
        
        self.story.append(Spacer(1, 0.2*inch))
    
    def _add_causal_chain(self):
        """Add causal chain section"""
        root_cause = self.rca_data.get('root_cause', {})
        causal_chain = root_cause.get('causal_chain', [])
        
        if not causal_chain:
            return
        
        self.story.append(Paragraph("Causal Chain", self.styles['SectionHeading']))
        
        for step in causal_chain:
            step_num = step.get('step', '?')
            time_utc = step.get('time_utc', 'N/A')
            service = step.get('service', 'Unknown')
            event = step.get('event', 'No event description')
            evidence = step.get('evidence', 'No evidence')
            
            step_text = f"""
            <b>Step {step_num}:</b> {service} at {time_utc} UTC<br/>
            <b>Event:</b> {event}<br/>
            <b>Evidence:</b> <font face="Courier" size="8">{evidence}</font>
            """
            self.story.append(Paragraph(step_text, self.styles['Normal']))
            self.story.append(Spacer(1, 0.1*inch))
        
        self.story.append(Spacer(1, 0.15*inch))
    
    def _add_evidence_section(self):
        """Add evidence section"""
        self.story.append(Paragraph("Evidence Summary", self.styles['SectionHeading']))
        
        evidence_list = self.rca_data.get('evidence', [])
        
        # Group by signal type
        by_signal = {}
        for ev in evidence_list:
            signal = ev.get('signal', 'unknown')
            if signal not in by_signal:
                by_signal[signal] = []
            by_signal[signal].append(ev.get('description', ''))
        
        for signal, descriptions in sorted(by_signal.items()):
            signal_title = signal.upper()
            self.story.append(Paragraph(f"<b>{signal_title}</b>", self.styles['Normal']))
            
            for desc in descriptions:
                desc_text = f"• {desc}"
                self.story.append(Paragraph(desc_text, self.styles['Evidence']))
            
            self.story.append(Spacer(1, 0.1*inch))
        
        self.story.append(Spacer(1, 0.15*inch))
    
    def _add_impact_section(self):
        """Add impact section"""
        self.story.append(Paragraph("Business Impact", self.styles['SectionHeading']))
        
        impact = self.rca_data.get('impact', 'No impact information available')
        self.story.append(Paragraph(impact, self.styles['BodyText']))
        
        self.story.append(Spacer(1, 0.2*inch))
    
    def _add_recommendations(self):
        """Add recommendations section"""
        suggested_fix = self.rca_data.get('suggested_fix', [])
        
        if not suggested_fix:
            return
        
        self.story.append(Paragraph("Recommended Actions", self.styles['SectionHeading']))
        
        for fix in suggested_fix:
            # Clean up numbering
            fix_text = fix.strip()
            if fix_text[0].isdigit() and '.' in fix_text[:3]:
                fix_text = fix_text.split('.', 1)[1].strip()
            
            self.story.append(Paragraph(f"• {fix_text}", self.styles['BodyText']))
            self.story.append(Spacer(1, 0.08*inch))
        
        self.story.append(Spacer(1, 0.2*inch))
    
    def _add_metadata(self):
        """Add metadata footer"""
        self.story.append(Spacer(1, 0.3*inch))
        
        metadata_text = f"""
        <font size="8" color="#999999">
        <b>Report Metadata</b><br/>
        Anomaly ID: {self.rca_data.get('anomaly_id', 'N/A')}<br/>
        Analysis Window: {self.rca_data.get('window_utc', {}).get('start', 'N/A')} to {self.rca_data.get('window_utc', {}).get('end', 'N/A')}<br/>
        Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}<br/>
        Source: RCA Agent Multi-Agent Analysis System
        </font>
        """
        self.story.append(Paragraph(metadata_text, self.styles['Normal']))
    
    def generate(self):
        """Generate the PDF report"""
        print(f"[PDF] Generating report for anomaly {self.rca_data['anomaly_id']}...")
        
        self._add_title_page()
        self._add_executive_summary()
        self._add_affected_services()
        self._add_root_cause_analysis()
        self._add_causal_chain()
        self._add_evidence_section()
        self._add_impact_section()
        self._add_recommendations()
        self._add_metadata()
        
        # Build PDF
        self.doc.build(self.story)
        
        print(f"[PDF] ✓ Report successfully generated: {self.output_path}")
        return str(self.output_path)


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python pdf_report_generator.py <rca_json_path> [output_pdf_path]")
        print("\nExample:")
        print("  python pdf_report_generator.py datasets/001-20260506T180913Z/rca-analysis.json")
        sys.exit(1)
    
    json_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    try:
        report = RCAPDFReport(json_path, output_path)
        pdf_path = report.generate()
        print(f"\n✓ PDF saved to: {pdf_path}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
