package com.example.cm015;

import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;


import androidx.appcompat.app.AppCompatActivity;



public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Button youtube=findViewById(R.id.you);
        youtube.setOnClickListener(new View.OnClickListener() {
        @Override
            public void onClick(View v) {
              Intent iobj=new Intent(Intent.ACTION_VIEW, Uri.parse("http://www.Youtube.com"));
              startActivity(iobj);
            }});
        Button factorial=findViewById(R.id.fact);
        factorial.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                EditText et=findViewById(R.id.et);
                int input=Integer.parseInt(et.getText().toString());
                TextView tv=findViewById(R.id.tv);
                int fact=1,i=1;
                while(i<=input){
                    fact=fact*i;
                    i=i+1;
                }
                tv.setText(+fact);
            }
        });

    }

}